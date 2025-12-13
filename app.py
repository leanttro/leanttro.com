import os
import json
import psycopg2
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import google.generativeai as genai

# Carrega variáveis de ambiente do arquivo .env
load_dotenv()

# --- CONFIGURAÇÃO INICIAL ---
app = Flask(__name__)
CORS(app)  # Permite que o Frontend (em outro domínio/porta) chame esta API

# Configuração do Google Gemini
GENAI_API_KEY = os.getenv("GEMINI_API_KEY")
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)
    # Modelo padrão para RAG e Chat
    model = genai.GenerativeModel('gemini-1.5-flash')

# Configuração do Banco de Dados
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "leanttro_db")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")

def get_db_connection():
    """Cria e retorna uma conexão com o PostgreSQL."""
    conn = psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )
    return conn

# --- SIMULAÇÃO DE CATÁLOGO (MOCK) ---
# Estrutura para separar "Caixa Rápido" (Setup) de "Recorrência" (Mensalidade)
CATALOGO_MOCK = {
    # PRODUTOS BASE
    "base_institucional": {"nome": "Site Institucional B2B", "setup": 1500.00, "mensal": 150.00},
    "base_ecommerce": {"nome": "E-commerce Solo", "setup": 2800.00, "mensal": 290.00},
    "base_eventos": {"nome": "Site de Casamento/Eventos", "setup": 900.00, "mensal": 0.00}, # Evento costuma ser one-off ou meses limitados
    
    # ADICIONAIS (UPSELL)
    "addon_chatbot": {"nome": "Chatbot Triagem (Evolution)", "setup": 500.00, "mensal": 100.00},
    "addon_rag": {"nome": "Vendedor IA (RAG)", "setup": 1200.00, "mensal": 200.00},
    "addon_dominio": {"nome": "Gestão de Domínio", "setup": 0.00, "mensal": 50.00}
}

# --- FUNÇÕES DE INFRAESTRUTURA ---
def setup_database():
    """
    Inicializa o banco de dados criando as tabelas necessárias e a extensão vetorial.
    Deve ser rodado ao iniciar a aplicação.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # 1. Habilita a extensão pgvector para o RAG
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        
        # 2. Tabela de Leads (Captura inicial)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(100),
                whatsapp VARCHAR(20),
                email VARCHAR(100),
                interesse VARCHAR(50),
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 3. Tabela de Vetores para RAG (Embeddings do Gemini geralmente têm 768 dimensões)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vetores_rag (
                id SERIAL PRIMARY KEY,
                conteudo TEXT,
                origem VARCHAR(50), -- ex: 'manual_produto', 'faq_institucional'
                embedding vector(768)
            );
        """)
        
        # 4. Tabela de Produtos (Estrutura futura para substituir o MOCK)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS produtos (
                id SERIAL PRIMARY KEY,
                codigo_slug VARCHAR(50) UNIQUE,
                nome VARCHAR(100),
                preco_setup DECIMAL(10, 2),
                preco_mensal DECIMAL(10, 2)
            );
        """)

        conn.commit()
        print("[INFO] Banco de dados inicializado com sucesso (Tabelas + pgvector).")
    except Exception as e:
        print(f"[ERRO] Falha ao inicializar DB: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

# --- ROTAS DA API ---

@app.route('/api/orcamento/calcular', methods=['POST'])
def calcular_orcamento():
    """
    Calcula o preço dinâmico baseado no Produto Base + Adicionais.
    Entrada: { "produto_base_id": "base_ecommerce", "adicionais_ids": ["addon_rag"] }
    """
    data = request.json
    base_id = data.get('produto_base_id')
    adicionais_ids = data.get('adicionais_ids', [])

    if base_id not in CATALOGO_MOCK:
        return jsonify({"erro": "Produto base inválido"}), 400

    # Pega valores do produto base
    produto_base = CATALOGO_MOCK[base_id]
    total_setup = produto_base['setup']
    total_mensal = produto_base['mensal']
    descricao_itens = [produto_base['nome']]

    # Soma os adicionais
    for add_id in adicionais_ids:
        if add_id in CATALOGO_MOCK:
            item = CATALOGO_MOCK[add_id]
            total_setup += item['setup']
            total_mensal += item['mensal']
            descricao_itens.append(item['nome'])

    return jsonify({
        "resumo": {
            "itens": descricao_itens,
            "total_setup_formatado": f"R$ {total_setup:.2f}",
            "total_mensal_formatado": f"R$ {total_mensal:.2f}",
            "valor_setup_raw": total_setup,
            "valor_mensal_raw": total_mensal
        },
        "mensagem": "Orçamento calculado com sucesso."
    })

@app.route('/api/leads/novo', methods=['POST'])
def novo_lead():
    """
    Salva um novo lead e dispara o gatilho para automação.
    """
    data = request.json
    nome = data.get('nome')
    whatsapp = data.get('whatsapp')
    email = data.get('email')
    interesse = data.get('interesse')

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            "INSERT INTO leads (nome, whatsapp, email, interesse) VALUES (%s, %s, %s, %s) RETURNING id",
            (nome, whatsapp, email, interesse)
        )
        lead_id = cur.fetchone()[0]
        conn.commit()

        # SIMULAÇÃO DE WEBHOOK N8N
        # Aqui usaríamos a biblioteca 'requests' para enviar um POST para o seu N8N
        print(f"--- [N8N TRIGGER] Disparando automação para Lead ID {lead_id} ({interesse}) ---")

        return jsonify({"status": "sucesso", "lead_id": lead_id, "mensagem": "Lead salvo e automação iniciada."}), 201

    except Exception as e:
        conn.rollback()
        return jsonify({"erro": str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/webhooks/whatsapp', methods=['POST'])
def webhook_whatsapp():
    """
    Endpoint placeholder para receber eventos da Evolution API (mensagens recebidas).
    """
    # data = request.json
    # print("Mensagem recebida do WhatsApp:", data)
    
    # Retornar 200 é crucial para o webhook não tentar reenviar
    return jsonify({"status": "recebido"}), 200

# --- INICIALIZAÇÃO ---
if __name__ == '__main__':
    # Garante que as tabelas existem antes de subir o servidor
    setup_database()
    # Debug=True apenas para desenvolvimento local
    app.run(host='0.0.0.0', port=5000, debug=True)