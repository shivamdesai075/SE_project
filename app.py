import atexit
import html
import re
import shutil
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import pdfplumber
import streamlit as st


APP_TITLE = "LegalLens India"
APP_SUBTITLE = (
    "Upload a legal or financial PDF to get an accuracy-first summary, risk flags, "
    "and a side-by-side simplification designed for Indian users."
)

SYSTEM_INSTRUCTION = """You are an accuracy-first legal document simplification assistant for Indian users.
Your job is to preserve legal meaning, flag risky obligations, and avoid creative rewriting.
Always prefer precise, cautious wording over fluent marketing language.
If the document is ambiguous, say so explicitly.
Assume the document may contain contracts, insurance policies, loan papers, employment terms, or service agreements.
Do not give final legal advice. Explain the text faithfully in simpler language."""

CHUNK_PROMPT_TEMPLATE = """You are Stage A: The Simplifier.

Task:
Read the legal/financial document excerpt below and simplify it without changing legal meaning.
Prioritize legal accuracy over creativity.
Do not omit duties, payment triggers, timelines, penalties, or conditions.

Output format:
1. Clause Title
2. Plain-English Explanation
3. User Action Required
4. Important Dates / Money / Conditions
5. Potential Concern

Document excerpt:
{chunk_text}
"""

FINAL_SUMMARY_PROMPT_TEMPLATE = """You are Stage B: The Aggregator.

You are given structured simplifications from multiple document chunks.
Create one consistent final summary with full-context awareness.
Resolve overlap carefully and do not introduce contradictions.
Preserve legal meaning from the source material.

Output format:
Summary & Actions:
- Bullet points telling the user what they must do, pay, sign, avoid, or watch closely.

Key Obligations:
- Bullet points

Important Dates / Amounts / Conditions:
- Bullet points

Simple Overall Summary:
- One concise paragraph

Chunk outputs:
{chunk_outputs}
"""

RISK_DETECTOR_PROMPT_TEMPLATE = """You are Stage C: The Auditor.

Review the full legal/financial document text and identify risky, unfair, or easily missed terms.
Focus on hidden penalties, broad indemnities, one-sided liability caps, auto-renewals, unilateral changes, aggressive recovery clauses, arbitration traps, data-sharing risks, foreclosure triggers, and vague termination rights.
Prioritize legal accuracy over creativity.

Output format:
Red Flags:
- Clause / issue
- Why it is risky
- Real-world impact on the user
- Severity: High / Medium / Low

Watch Carefully:
- Any clause that may become risky depending on facts

Full document text:
{full_text}
"""

HINGLISH_PROMPT_TEMPLATE = """You are Stage D: The Translator.

Convert the final output into easy Hinglish for Indian users.
Keep all legal meaning intact.
Do not remove warnings, dates, amounts, or obligations.
Keep the structure readable with bullets and short lines.

Text to convert:
{text}
"""

ACCURACY_CHECK_PROMPT_TEMPLATE = """You are Stage E: The Quality Check.

Compare the original document text against the AI-generated summary and risk output.
Your goal is to ensure no legal meaning was lost, softened, or incorrectly added.
Prioritize accuracy over style.

Output format:
Accuracy Verdict:
- Pass / Needs Review

Potential Mismatches:
- Bullet points

Missing Nuance:
- Bullet points

Safe To Rely On For First Read?
- Yes / With Caution / No

Original text:
{original_text}

AI output:
{ai_output}
"""

GLOSSARY: Dict[str, str] = {
    "Indemnity": "A promise to cover someone else's loss, damage, or legal cost.",
    "Liability": "Legal responsibility for loss, damage, or payment.",
    "Force Majeure": "Unexpected events beyond control, like natural disasters, that may excuse delays.",
    "Arbitration": "A private dispute process instead of going to court.",
    "Jurisdiction": "The place or court that will handle legal disputes.",
    "Default": "Failure to do what the agreement requires, such as missing a payment.",
    "Termination": "The contract ending before full completion.",
    "Penalty": "An extra charge or consequence for breaking a rule or condition.",
    "Waiver": "When someone chooses not to enforce a right for that moment.",
    "Confidentiality": "A duty to keep certain information private.",
}

TOKEN_TARGET_MIN = 1000
TOKEN_TARGET_MAX = 1500

LEGAL_TERMS_MAP = {
    "shall": "must",
    "hereinafter": "from now on",
    "forthwith": "immediately",
    "prior to": "before",
    "terminate": "end",
    "indemnify": "cover losses of",
    "liable": "legally responsible",
    "notwithstanding": "despite that",
    "pursuant to": "under",
    "thereof": "of it",
}

RISK_PATTERNS = [
    ("High", "Indemnity obligation", ["indemnity", "indemnify", "hold harmless"]),
    ("High", "Penalty or liquidated damages", ["penalty", "liquidated damages", "late fee", "default interest"]),
    ("High", "Unilateral change rights", ["sole discretion", "may amend", "without notice", "change any term"]),
    ("High", "Broad termination or recall rights", ["terminate at any time", "recall", "accelerate", "immediate termination"]),
    ("Medium", "Auto-renewal or rollover", ["auto-renew", "automatically renew", "rollover"]),
    ("Medium", "Arbitration / jurisdiction constraint", ["arbitration", "exclusive jurisdiction", "seat of arbitration"]),
    ("Medium", "Data sharing / disclosure", ["share your information", "third party", "affiliate", "disclose"]),
    ("Medium", "Waiver of rights", ["waive", "waiver", "relinquish"]),
    ("Medium", "Liability cap or exclusion", ["liability shall not exceed", "no liability", "exclude liability"]),
    ("Low", "Strict notice or deadline", ["within 7 days", "within 15 days", "written notice", "immediately notify"]),
]

ACTION_KEYWORDS = ["must", "shall", "required", "need to", "obligated", "pay", "submit", "notify", "maintain"]
IMPORTANT_KEYWORDS = ["rs.", "inr", "%", "days", "months", "years", "interest", "fee", "charge", "date", "within"]


@dataclass
class ChunkResult:
    index: int
    text: str
    simplification: str


def init_session_state() -> None:
    defaults = {
        "session_id": str(uuid.uuid4()),
        "uploaded_file_path": None,
        "session_temp_dir": tempfile.mkdtemp(prefix="legal_lens_"),
        "pipeline_cache": {},
        "results": None,
        "last_uploaded_name": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def scrub_file(file_path: Optional[str]) -> None:
    if not file_path:
        return
    try:
        path = Path(file_path)
        if path.exists():
            path.unlink()
    except OSError:
        pass


def scrub_session_artifacts() -> None:
    scrub_file(st.session_state.get("uploaded_file_path"))
    temp_dir = st.session_state.get("session_temp_dir")
    if temp_dir:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except OSError:
            pass
    st.session_state["session_temp_dir"] = tempfile.mkdtemp(prefix="legal_lens_")
    st.session_state["uploaded_file_path"] = None
    st.session_state["results"] = None
    st.session_state["pipeline_cache"] = {}
    st.session_state["last_uploaded_name"] = None


def _atexit_cleanup() -> None:
    temp_root = Path(tempfile.gettempdir())
    for path in temp_root.glob("legal_lens_*"):
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_atexit_cleanup)


def save_uploaded_pdf(uploaded_file) -> str:
    if st.session_state.get("uploaded_file_path"):
        scrub_file(st.session_state["uploaded_file_path"])

    temp_dir = Path(st.session_state["session_temp_dir"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / f"{uuid.uuid4()}_{uploaded_file.name}"
    file_path.write_bytes(uploaded_file.getbuffer())
    st.session_state["uploaded_file_path"] = str(file_path)
    st.session_state["last_uploaded_name"] = uploaded_file.name
    return str(file_path)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_pymupdf(file_path: str) -> str:
    pages: List[str] = []
    with fitz.open(file_path) as doc:
        for page in doc:
            blocks = page.get_text("blocks")
            blocks = sorted(blocks, key=lambda block: (round(block[1], 1), round(block[0], 1)))
            page_text = "\n".join(block[4].strip() for block in blocks if len(block) > 4 and block[4].strip())
            pages.append(page_text)
    return normalize_whitespace("\n\n".join(pages))


def extract_text_pdfplumber(file_path: str) -> str:
    pages: List[str] = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text(layout=True) or ""
            if page_text.strip():
                pages.append(page_text)
    return normalize_whitespace("\n\n".join(pages))


def extract_document_text(file_path: str) -> str:
    primary_text = extract_text_pymupdf(file_path)
    if len(primary_text) >= 500:
        return primary_text

    fallback_text = extract_text_pdfplumber(file_path)
    return fallback_text if len(fallback_text) > len(primary_text) else primary_text


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def sentence_split(text: str) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", text.strip())
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def split_large_paragraph(paragraph: str, token_limit: int) -> List[str]:
    sentences = sentence_split(paragraph)
    if not sentences:
        return [paragraph]

    parts: List[str] = []
    current: List[str] = []
    for sentence in sentences:
        candidate = " ".join(current + [sentence]).strip()
        if current and estimate_tokens(candidate) > token_limit:
            parts.append(" ".join(current).strip())
            current = [sentence]
        else:
            current.append(sentence)
    if current:
        parts.append(" ".join(current).strip())
    return parts


def semantic_chunk_text(text: str, min_tokens: int = TOKEN_TARGET_MIN, max_tokens: int = TOKEN_TARGET_MAX) -> List[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    normalized_parts: List[str] = []

    for paragraph in paragraphs:
        if estimate_tokens(paragraph) > max_tokens:
            normalized_parts.extend(split_large_paragraph(paragraph, max_tokens))
        else:
            normalized_parts.append(paragraph)

    chunks: List[str] = []
    current_parts: List[str] = []

    for part in normalized_parts:
        candidate = "\n\n".join(current_parts + [part]).strip()
        candidate_tokens = estimate_tokens(candidate)

        if current_parts and candidate_tokens > max_tokens:
            current_chunk = "\n\n".join(current_parts).strip()
            chunks.append(current_chunk)
            current_parts = [part]
        else:
            current_parts.append(part)

        current_text = "\n\n".join(current_parts).strip()
        if estimate_tokens(current_text) >= min_tokens:
            chunks.append(current_text)
            current_parts = []

    if current_parts:
        remainder = "\n\n".join(current_parts).strip()
        if chunks and estimate_tokens(remainder) < max(300, min_tokens // 2):
            chunks[-1] = f"{chunks[-1]}\n\n{remainder}".strip()
        else:
            chunks.append(remainder)

    return [chunk for chunk in chunks if chunk.strip()]


def clean_sentence(sentence: str) -> str:
    sentence = normalize_whitespace(sentence)
    for source, target in LEGAL_TERMS_MAP.items():
        sentence = re.sub(rf"\b{re.escape(source)}\b", target, sentence, flags=re.IGNORECASE)
    return sentence


def sentence_score(sentence: str) -> int:
    lowered = sentence.lower()
    score = 0
    if any(keyword in lowered for keyword in ACTION_KEYWORDS):
        score += 3
    if any(keyword in lowered for keyword in IMPORTANT_KEYWORDS):
        score += 2
    if re.search(r"\b\d+\b", sentence):
        score += 1
    if any(term.lower() in lowered for term in GLOSSARY):
        score += 1
    return score


def top_sentences(text: str, limit: int = 3) -> List[str]:
    sentences = sentence_split(text)
    ranked = sorted(sentences, key=lambda sentence: (sentence_score(sentence), len(sentence)), reverse=True)
    unique: List[str] = []
    for sentence in ranked:
        normalized = sentence.lower()
        if normalized not in {entry.lower() for entry in unique}:
            unique.append(sentence)
        if len(unique) == limit:
            break
    return unique or sentences[:limit]


def extract_amounts_dates_conditions(text: str, limit: int = 4) -> List[str]:
    lines = sentence_split(text)
    matches = [
        line for line in lines
        if re.search(r"(rs\.|inr|\b\d+%|\b\d+\s+(days?|months?|years?)|\binterest\b|\bfee\b|\bcharge\b)", line, re.IGNORECASE)
    ]
    return matches[:limit]


def detect_risks_in_text(text: str, limit: int = 6) -> List[Tuple[str, str, str]]:
    sentences = sentence_split(text)
    findings: List[Tuple[str, str, str]] = []
    for severity, label, keywords in RISK_PATTERNS:
        for sentence in sentences:
            lowered = sentence.lower()
            if any(keyword in lowered for keyword in keywords):
                findings.append((severity, label, sentence))
                break
        if len(findings) >= limit:
            break
    return findings


def stage_a_simplify_chunk(chunk_text: str, chunk_index: int) -> str:
    title = top_sentences(chunk_text, limit=1)[0][:80].rstrip(".")
    summary_points = [f"- {clean_sentence(sentence)}" for sentence in top_sentences(chunk_text, limit=3)]

    actions = [
        f"- {clean_sentence(sentence)}"
        for sentence in sentence_split(chunk_text)
        if any(keyword in sentence.lower() for keyword in ACTION_KEYWORDS)
    ][:3]
    if not actions:
        actions = ["- No direct user action was clearly detected in this section."]

    money_dates = [f"- {clean_sentence(item)}" for item in extract_amounts_dates_conditions(chunk_text)]
    if not money_dates:
        money_dates = ["- No specific amount, date, or condition was confidently detected in this section."]

    concerns = [
        f"- {severity}: {label}. Source line: {clean_sentence(sentence)}"
        for severity, label, sentence in detect_risks_in_text(chunk_text, limit=2)
    ]
    if not concerns:
        concerns = ["- No immediate high-risk term was detected in this chunk, but context may still matter."]

    return "\n".join([
        f"1. Clause Title\n{title}",
        "2. Plain-English Explanation",
        *summary_points,
        "3. User Action Required",
        *actions,
        "4. Important Dates / Money / Conditions",
        *money_dates,
        "5. Potential Concern",
        *concerns,
    ])


def stage_b_aggregate(stage_a_results: List[ChunkResult], full_text: str) -> str:
    all_sentences = [sentence for result in stage_a_results for sentence in sentence_split(result.text)]
    obligations = [sentence for sentence in all_sentences if any(keyword in sentence.lower() for keyword in ACTION_KEYWORDS)]
    important = [sentence for sentence in all_sentences if sentence in extract_amounts_dates_conditions(" ".join(all_sentences), limit=10)]
    themes = Counter(
        word.lower()
        for word in re.findall(r"\b[A-Za-z][A-Za-z\-]+\b", full_text)
        if len(word) > 5 and word.lower() not in {"within", "should", "thereof", "hereunder", "agreement"}
    )
    theme_summary = ", ".join(word for word, _ in themes.most_common(5))

    summary_actions = [f"- {clean_sentence(sentence)}" for sentence in obligations[:6]]
    if not summary_actions:
        summary_actions = ["- Read each payment, notice, and termination clause carefully before acting."]

    key_obligations = [f"- {clean_sentence(sentence)}" for sentence in top_sentences(" ".join(obligations) or full_text, limit=4)]
    important_points = [f"- {clean_sentence(sentence)}" for sentence in important[:5]]
    if not important_points:
        important_points = ["- Specific dates or amounts were limited; review the original text side-by-side."]

    overall = (
        "This document appears to focus on "
        f"{theme_summary or 'payment, liability, and compliance duties'}. "
        "The simplified view below is generated locally and keeps close to the source wording, "
        "so the user should still review the original clauses before relying on it."
    )

    return "\n".join([
        "Summary & Actions:",
        *summary_actions,
        "",
        "Key Obligations:",
        *key_obligations,
        "",
        "Important Dates / Amounts / Conditions:",
        *important_points,
        "",
        "Simple Overall Summary:",
        f"- {overall}",
    ])


def stage_c_audit(full_text: str) -> str:
    findings = detect_risks_in_text(full_text, limit=8)
    red_flags: List[str] = []
    for severity, label, sentence in findings:
        impact = "This could increase cost, legal exposure, or reduce negotiation power."
        red_flags.extend([
            f"- Clause / issue: {label}",
            f"- Why it is risky: The wording suggests a {label.lower()}.",
            f"- Real-world impact on the user: {impact}",
            f"- Severity: {severity}",
            f"- Source text: {clean_sentence(sentence)}",
            "",
        ])
    if not red_flags:
        red_flags = ["- No obvious risk pattern was auto-detected. Manual review is still recommended."]

    watch_list = [
        "- Cross-check penalty, termination, indemnity, and dispute resolution clauses manually.",
        "- Confirm whether any annexures, schedules, or tables were missing from the PDF extraction.",
    ]

    return "\n".join(["Red Flags:", *red_flags, "Watch Carefully:", *watch_list])


def convert_to_hinglish(text: str) -> str:
    replacements = {
        "must": "zaroor karna hoga",
        "should": "dhyan se karna chahiye",
        "pay": "payment karna",
        "legally responsible": "kanuni taur par zimmedar",
        "end": "khatam",
        "before": "pehle",
        "within": "itne time ke andar",
        "review": "dhyan se dekhna",
        "risk": "risk",
        "notice": "notice",
    }
    converted = text
    for source, target in replacements.items():
        converted = re.sub(rf"\b{re.escape(source)}\b", target, converted, flags=re.IGNORECASE)
    return "Hinglish Version:\n" + converted


def stage_e_accuracy_check(original_text: str, ai_output: str) -> str:
    original_numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", original_text))
    output_numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", ai_output))
    missing_numbers = sorted(original_numbers - output_numbers)
    original_terms = {term for term in GLOSSARY if re.search(rf"\b{re.escape(term)}\b", original_text, re.IGNORECASE)}
    output_terms = {term for term in GLOSSARY if re.search(rf"\b{re.escape(term)}\b", ai_output, re.IGNORECASE)}
    missing_terms = sorted(original_terms - output_terms)

    mismatches = []
    if missing_numbers:
        mismatches.append(f"- Some numbers or percentages from the original do not appear in the summary: {', '.join(missing_numbers[:8])}")
    if missing_terms:
        mismatches.append(f"- Some legal concepts may need a manual cross-check: {', '.join(missing_terms[:6])}")
    if not mismatches:
        mismatches.append("- No major numeric or glossary-level mismatch was auto-detected.")

    nuance = [
        "- The offline simplifier keeps the source close, but it does not reason like a lawyer.",
        "- Always verify annexures, tables, handwritten notes, and signatures manually.",
    ]

    verdict = "Pass" if not missing_numbers else "Needs Review"
    reliance = "Yes" if verdict == "Pass" else "With Caution"
    return "\n".join([
        "Accuracy Verdict:",
        f"- {verdict}",
        "",
        "Potential Mismatches:",
        *mismatches,
        "",
        "Missing Nuance:",
        *nuance,
        "",
        "Safe To Rely On For First Read?",
        f"- {reliance}",
    ])


def highlight_glossary_terms(text: str) -> str:
    highlighted = html.escape(text)
    for term, meaning in GLOSSARY.items():
        pattern = re.compile(rf"\b({re.escape(term)})\b", re.IGNORECASE)
        highlighted = pattern.sub(
            lambda match: (
                f"<span style='background-color:#fff3bf;padding:0 2px;border-radius:3px;' "
                f"title='{html.escape(meaning, quote=True)}'><strong>{match.group(0)}</strong></span>"
            ),
            highlighted,
        )
    highlighted = highlighted.replace("\n", "<br>")
    return highlighted


def render_sidebar():
    st.sidebar.header("Settings")
    st.sidebar.success("Offline mode enabled")
    st.sidebar.caption("This version runs locally and does not require any API key.")
    hinglish_toggle = st.sidebar.toggle("Translate final output to Hinglish")

    st.sidebar.divider()
    st.sidebar.subheader("Glossary")
    for term, meaning in GLOSSARY.items():
        with st.sidebar.expander(term):
            st.write(meaning)

    st.sidebar.divider()
    if st.sidebar.button("Scrub Session Files"):
        scrub_session_artifacts()
        st.sidebar.success("Temporary files deleted.")

    st.sidebar.caption(
        "Uploaded PDFs are stored in a temporary session folder and deleted when scrubbed or replaced."
    )
    return hinglish_toggle


def validate_inputs(uploaded_file) -> bool:
    if not uploaded_file:
        st.info("Upload a PDF to begin.")
        return False
    return True


def run_pipeline(file_path: str, translate_to_hinglish: bool):
    status = st.empty()
    progress = st.progress(0, text="Waiting to start")

    status.info("Extracting text from PDF...")
    progress.progress(10, text="Extracting Text")
    original_text = extract_document_text(file_path)
    if not original_text:
        raise ValueError("No readable text was extracted from this PDF.")

    status.info("Building semantic chunks...")
    progress.progress(25, text="Preparing Chunks")
    chunks = semantic_chunk_text(original_text)

    stage_a_results: List[ChunkResult] = []
    total_chunks = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        progress_value = 25 + int((idx / max(total_chunks, 1)) * 30)
        progress.progress(progress_value, text=f"Analyzing Clauses ({idx}/{total_chunks})")
        simplification = stage_a_simplify_chunk(chunk, idx)
        result = ChunkResult(index=idx, text=chunk, simplification=simplification)
        stage_a_results.append(result)
        st.session_state["pipeline_cache"][f"chunk_{idx}"] = {
            "source_text": chunk,
            "simplified_output": simplification,
        }

    status.info("Aggregating final summary...")
    progress.progress(65, text="Creating Final Summary")
    final_summary = stage_b_aggregate(stage_a_results, original_text)

    status.info("Checking risks across full document...")
    progress.progress(78, text="Checking Risks")
    risk_report = stage_c_audit(original_text)

    translated_summary = None
    translated_risks = None
    if translate_to_hinglish:
        status.info("Translating into Hinglish...")
        progress.progress(88, text="Translating Output")
        translated_summary = convert_to_hinglish(final_summary)
        translated_risks = convert_to_hinglish(risk_report)

    status.info("Running accuracy check...")
    progress.progress(96, text="Validating Accuracy")
    final_ai_output = "\n\n".join(
        part for part in [final_summary, risk_report, translated_summary, translated_risks] if part
    )
    accuracy_report = stage_e_accuracy_check(original_text, final_ai_output)

    progress.progress(100, text="Done")
    status.success("Analysis complete.")

    return {
        "original_text": original_text,
        "chunks": chunks,
        "stage_a_results": stage_a_results,
        "final_summary": final_summary,
        "risk_report": risk_report,
        "translated_summary": translated_summary,
        "translated_risks": translated_risks,
        "accuracy_report": accuracy_report,
    }


def render_results(results: Dict, translate_to_hinglish: bool) -> None:
    st.subheader("Results Dashboard")
    st.caption("Accuracy-first output. Please use this as a guided first read, not as final legal advice.")

    tab1, tab2, tab3 = st.tabs(["Summary & Actions", "Red Flags", "Original vs. Simple"])

    with tab1:
        summary_to_show = results["translated_summary"] if translate_to_hinglish else results["final_summary"]
        st.markdown(highlight_glossary_terms(summary_to_show), unsafe_allow_html=True)
        st.divider()
        st.subheader("Accuracy Check")
        st.markdown(highlight_glossary_terms(results["accuracy_report"]), unsafe_allow_html=True)

    with tab2:
        risk_to_show = results["translated_risks"] if translate_to_hinglish and results["translated_risks"] else results["risk_report"]
        st.error("Review these clauses carefully before signing or paying.")
        st.markdown(highlight_glossary_terms(risk_to_show), unsafe_allow_html=True)

    with tab3:
        for chunk_result in results["stage_a_results"]:
            with st.expander(f"Chunk {chunk_result.index}"):
                left_col, right_col = st.columns(2)
                with left_col:
                    st.markdown("**Original**")
                    st.text_area(
                        f"Original Chunk {chunk_result.index}",
                        value=chunk_result.text,
                        height=240,
                        key=f"orig_{chunk_result.index}",
                    )
                with right_col:
                    st.markdown("**Simplified**")
                    st.text_area(
                        f"Simplified Chunk {chunk_result.index}",
                        value=chunk_result.simplification,
                        height=240,
                        key=f"simp_{chunk_result.index}",
                    )


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="⚖️", layout="wide")
    init_session_state()

    st.title(APP_TITLE)
    st.write(APP_SUBTITLE)

    hinglish_toggle = render_sidebar()

    uploaded_file = st.file_uploader("Upload a legal or financial PDF", type=["pdf"])
    process_clicked = st.button("Run Simplification Pipeline", type="primary")

    if process_clicked and validate_inputs(uploaded_file):
        st.session_state["pipeline_cache"] = {}
        try:
            file_path = save_uploaded_pdf(uploaded_file)
            results = run_pipeline(file_path=file_path, translate_to_hinglish=hinglish_toggle)
            st.session_state["results"] = results
        except Exception as exc:
            st.error(f"Pipeline failed: {exc}")
        finally:
            scrub_file(st.session_state.get("uploaded_file_path"))
            st.session_state["uploaded_file_path"] = None

    if st.session_state.get("results"):
        render_results(st.session_state["results"], hinglish_toggle)


if __name__ == "__main__":
    main()
