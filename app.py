import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
import mercadopago
import requests
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- CONFIGURAÇÕES GLOBAIS ---
# 1. Banco de Dados
DB_URL = os.getenv('DATABASE_URL')

# 2. IA (Gemini)
GEMINI_KEY = os.getenv('GOOGLE_API_KEY')
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

# 3. Mercado Pago
MP_ACCESS_TOKEN = os.getenv('MP_ACCESS_TOKEN')
mp_sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# 4. Evolution API (Para envio de mensagens no Whats)
EVOLUTION_URL = os.getenv('EVOLUTION_API_URL') # Ex: https://evo.leanttro.com
EVOLUTION_KEY = os.getenv('EVOLUTION_API_KEY')

# --- FUNÇÕES AUXILIARES ---
def get_db_connection():
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        print(f"❌ Erro DB: {e}")
        return None

def get_embedding(text):
    """Gera o vetor numérico do texto usando Gemini"""
    if not GEMINI_KEY: return None
    try:
        result = genai.embed_content(
            model="models/embedding-001",
            content=text,
            task_type="retrieval_document",
            title="Q&A"
        )
        return result['embedding']
    except Exception as e:
        print(f"❌ Erro Embedding: {e}")
        return None

# --- ROTAS DE PÁGINAS (FRONTEND) ---

@app.route('/')
def home():
    # A vitrine principal
    return render_template('index.html')

@app.route('/login')
def login_page():
    # Tela de acesso do cliente
    return render_template('login.html')

@app.route('/admin')
def admin_page():
    # O antigo 'dashboard' agora é Admin
    # Futuramente: Adicionar verificação de login aqui
    return render_template('admin.html')

# --- ROTAS DE API (BACKEND) ---

# 1. CAPTURA DE LEADS (Salva quem tentou comprar)
@app.route('/api/leads', methods=['POST'])
def save_lead():
    data = request.json
    name = data.get('name')
    whatsapp = data.get('whatsapp')
    interest = data.get('interest') # Qual produto ele estava vendo

    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro DB"}), 500

    try:
        cur = conn.cursor()
        # Salva o lead na tabela clients
        cur.execute("""
            INSERT INTO clients (name, whatsapp, status, company_name) 
            VALUES (%s, %s, 'lead', %s)
            RETURNING id
        """, (name, whatsapp, interest))
        conn.commit()
        return jsonify({"message": "Lead salvo com sucesso!"}), 201
    except Exception as e:
        print(f"Erro ao salvar lead: {e}")
        return jsonify({"error": "Erro ao salvar lead"}), 500
    finally:
        conn.close()

# 2. CHECKOUT (Gera Link do Mercado Pago)
@app.route('/api/checkout/create', methods=['POST'])
def create_checkout():
    if not mp_sdk:
        return jsonify({"error": "Mercado Pago não configurado"}), 500

    data = request.json
    product_title = data.get('product_title', 'Projeto Leanttro')
    total_setup = float(data.get('total_setup', 0))
    
    # Cria a preferência de pagamento
    preference_data = {
        "items": [
            {
                "title": f"SETUP: {product_title}",
                "quantity": 1,
                "unit_price": total_setup
            }
        ],
        "back_urls": {
            "success": "https://leanttro.com/admin", # Redireciona para o painel após pagar
            "failure": "https://leanttro.com/",
            "pending": "https://leanttro.com/"
        },
        "auto_return": "approved",
        "statement_descriptor": "LEANTTRO TECH"
    }

    try:
        preference_response = mp_sdk.preference().create(preference_data)
        payment_url = preference_response["response"]["init_point"]
        
        return jsonify({
            "checkout_url": payment_url, 
            "message": "Checkout criado"
        })
    except Exception as e:
        print(f"Erro MP: {e}")
        return jsonify({"error": str(e)}), 500

# 3. WEBHOOK EVOLUTION (Cérebro da IA no WhatsApp)
@app.route('/webhooks/evolution', methods=['POST'])
def evolution_webhook():
    data = request.json
    print("📩 Webhook Recebido:", data)

    # Lógica de RAG (Busca Vetorial + Gemini)
    # Ative esta parte quando configurar a Evolution API
    """
    try:
        msg_text = data['data']['message']['conversation']
        remote_jid = data['data']['key']['remoteJid']
        
        # 1. Gera Embedding da pergunta
        vector = get_embedding(msg_text)
        
        # 2. Busca no Banco Vetorial
        conn = get_db_connection()
        cur = conn.cursor()
        # Busca os 2 trechos mais parecidos
        cur.execute("SELECT content FROM leanttro_rag_knowledge ORDER BY embedding <=> %s::vector LIMIT 2", (vector,))
        rows = cur.fetchall()
        contexto = " ".join([r[0] for r in rows]) if rows else "Sem contexto específico."
        
        # 3. Gera Resposta com Gemini usando o contexto
        model = genai.GenerativeModel('gemini-pro')
        prompt = f"Você é o assistente da Leanttro. Use este contexto: {contexto}. Responda à pergunta: {msg_text}"
        response = model.generate_content(prompt)
        resposta_final = response.text
        
        # 4. Envia de volta para o WhatsApp
        if EVOLUTION_URL and EVOLUTION_KEY:
            requests.post(f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_KEY}", json={
                "number": remote_jid,
                "text": resposta_final
            })
        
    except Exception as e:
        print(f"Erro no fluxo IA: {e}")
    """

    return jsonify({"status": "received"}), 200

# 4. LISTAR PRODUTOS (Para o Painel Admin futuramente)
@app.route('/api/products', methods=['GET'])
def list_products():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro DB"}), 500
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name, price_setup, price_monthly FROM products WHERE is_active = TRUE")
        return jsonify(cur.fetchall())
    finally:
        conn.close()

if __name__ == '__main__':
    # Roda na porta 5000 (Padrão Flask/Dokploy)
    app.run(host='0.0.0.0', port=5000)

# --- ROTA: CHATBOT DO SITE (Widget) ---
@app.route('/api/chat/message', methods=['POST'])
def chat_message():
    data = request.json
    user_message = data.get('message')
    
    # 1. Recupera Contexto (RAG Simplificado)
    contexto_vendas = """
    Você é o Assistente Virtual da Leanttro.
    Sua missão é vender sites e softwares. Seja curto, persuasivo e use emojis.
    
    Nossos Produtos:
    1. Site Institucional (R$ 499): Para advogados, clínicas. Passa autoridade.
    2. Loja Virtual (R$ 999): Sem taxas, com painel admin e integração Mercado Livre.
    3. Site de Casamento (R$ 399): Lista de presentes em dinheiro (PIX).
    4. Projetos Corp (A partir de R$ 1.500): Automação, Dashboards e IA.
    
    Se o cliente perguntar preço, fale o valor e convide para fechar contrato.
    Se o cliente tiver dúvida técnica, explique de forma simples.
    Sempre termine a resposta incentivando a clicar em "Solicitar Contrato" ou chamando para o WhatsApp.
    """

    try:
        # 2. Consulta o Gemini
        model = genai.GenerativeModel('gemini-pro')
        prompt = f"Contexto: {contexto_vendas}\nCliente: {user_message}\nMaia:"
        
        response = model.generate_content(prompt)
        bot_reply = response.text
        
        return jsonify({"reply": bot_reply})

    except Exception as e:
        print(f"Erro Gemini: {e}")
        return jsonify({"reply": "Ops! Tive um pico de energia aqui. ⚡ Pode me chamar no WhatsApp?"})