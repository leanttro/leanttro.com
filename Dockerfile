# Usa a imagem oficial do Nginx (versão leve Alpine)
FROM nginx:alpine

# Remove a página padrão do Nginx para limpar a casa
RUN rm -rf /usr/share/nginx/html/*

# Copia todos os seus arquivos (index.html e imagens) para a pasta pública do Nginx
COPY . /usr/share/nginx/html

# Expõe a porta 80 (padrão web)
EXPOSE 80

# Inicia o servidor Nginx
CMD ["nginx", "-g", "daemon off;"]