# Usa uma imagem oficial do Python em vez do Nginx
FROM python:3.9-slim

# Define o diretório de trabalho
WORKDIR /app

# Instala dependências do sistema necessárias para compilar pacotes (como psycopg2)
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

# Copia o arquivo de dependências
COPY requirements.txt .

# Instala as bibliotecas Python listadas no requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o código do projeto para dentro do container
COPY . .

# Cria as pastas necessárias caso não existam (segurança)
RUN mkdir -p templates static

# Expõe a porta 5000 (onde o Flask/Gunicorn vai rodar)
EXPOSE 5000

# Comando para iniciar o servidor Gunicorn
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]