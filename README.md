# LegalLens India

LegalLens India is a privacy-first Streamlit app that simplifies Indian legal and financial PDFs into plain-language summaries, red-flag alerts, side-by-side clause comparisons, Hinglish output, and glossary-assisted explanations.

## What It Does

- Extracts text from uploaded PDFs using `PyMuPDF` with `pdfplumber` as fallback.
- Splits long documents into semantic chunks so clauses are less likely to break mid-thought.
- Builds a multi-stage local analysis pipeline for:
  - clause simplification
  - document summary and action items
  - risky clause detection
  - Hinglish conversion
  - basic accuracy checks
- Highlights important legal terms like `Indemnity`, `Liability`, and `Arbitration`.
- Deletes uploaded PDF files after processing for better privacy.

## Tech Stack

- Python
- Streamlit
- PyMuPDF
- pdfplumber

## Project Structure

```text
.
├── app.py
├── requirements.txt
├── README.md
└── .gitignore
```

## Run Locally

1. Clone the repository:

```bash
git clone https://github.com/shivamdesai075/SE_project.git
cd SE_project
```

2. Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows:

```bash
.venv\Scripts\activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Start the app:

```bash
streamlit run app.py
```

5. Open the local URL shown by Streamlit, usually:

```text
http://localhost:8501
```

## How To Use

1. Upload a legal or financial PDF.
2. Click `Run Simplification Pipeline`.
3. Review the output tabs:
   - `Summary & Actions`
   - `Red Flags`
   - `Original vs. Simple`
4. Optionally turn on the `Translate final output to Hinglish` toggle.
5. Use the glossary in the sidebar for quick legal term explanations.

## Deployment

### Streamlit Community Cloud

1. Push this repository to GitHub.
2. Go to [Streamlit Community Cloud](https://streamlit.io/cloud).
3. Create a new app and select this repository.
4. Set:
   - Branch: `main`
   - Main file path: `app.py`
5. Deploy the app.

### Hugging Face Spaces

1. Create a new Space.
2. Choose `Streamlit` as the SDK.
3. Connect or upload this repository.
4. Ensure `requirements.txt` is present.
5. Launch the Space.

## Privacy Notes

- Uploaded PDFs are stored in a temporary session directory.
- The app scrubs uploaded files after processing.
- The current version runs offline and does not require an API key.

## Important Limitation

This version uses local heuristic analysis instead of a remote LLM. That improves privacy and removes API setup, but it is less nuanced than a true AI legal-review pipeline. Users should treat the output as a guided first read, not final legal advice.

## Future Improvements

- OCR for scanned PDFs
- Export to PDF or DOCX
- API-backed advanced legal summarization mode
- Better clause classification for Indian loan, insurance, and employment documents
