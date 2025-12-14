# Usa uma imagem leve do Python
FROM python:3.11-slim

# Define o diretório de trabalho dentro do container
WORKDIR /app

# Instala as dependências do sistema necessárias para o Postgres
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

# Copia os arquivos de requisitos e instala
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o resto do código para dentro da pasta /app
COPY . .

# Expõe a porta 5000 (Padrão do Flask)
EXPOSE 5000

# Comando para rodar a aplicação em produção usando Gunicorn
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]