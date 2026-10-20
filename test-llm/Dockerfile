FROM python:3.12-slim
RUN pip install --no-cache-dir pdfplumber openpyxl
WORKDIR /app
ENTRYPOINT ["python", "test_llm_facture.py"]
