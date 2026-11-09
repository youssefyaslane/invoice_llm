# Application de factures (Flask + Docling) servie par gunicorn sur le port 6001
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app/app_invoice
COPY requirements.txt contraintes.txt ./
# torch en version CPU d'abord : sinon pip installe la version CUDA (plusieurs Go inutiles sans carte graphique).
# Puis le reste aux versions exactes de .venv-docling : une autre version de Docling lit le PDF un peu autrement
# et le modèle répond différemment.
RUN pip install --no-cache-dir torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt -c contraintes.txt
COPY app.py .
COPY templates templates
COPY static static
EXPOSE 6001
# un seul worker (la file d'attente est en mémoire), plusieurs threads pour les pages pendant qu'une facture est lue
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:6001", "--timeout", "120", \
     "--access-logfile", "-", "app:creer_app()"]
