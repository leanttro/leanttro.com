import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

# --- CONFIGURAÇÃO FLASK ---
# removemos o template_folder='.' para ele usar a pasta /templates corretamente
app = Flask(__name__)
CORS(app) # Permite que o frontend chame a API

# --- CONFIGURAÇÃO BANCO DE DADOS ---
def get_db_connection():
    try:
        # Pega a URL do ambiente (aquela que colocamos no Dokploy)
        db_url = os.getenv('DATABASE_URL')
        if not db_url:
            print("❌ ERRO: DATABASE_URL não encontrada.")
            return None
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        print(f"❌ Erro de Conexão com Banco: {e}")
        return None

# --- CONFIGURAÇÃO IA (GEMINI) ---
GEMINI_KEY = os.getenv('GOOGLE_API_KEY')
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

# --- ROTA 1: A HOME (Carrega o Site) ---
@app.route('/')
def home():
    # O Flask vai buscar automaticamente dentro da pasta 'templates'
    return render_template('index.html')

# --- ROTA 2: LISTAR PRODUTOS (Para a Vitrine) ---
@app.route('/api/products', methods=['GET'])
def list_products():
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro no banco"}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Busca produtos ativos
        cur.execute("SELECT id, name, slug, price_setup, price_monthly, features FROM products WHERE is_active = TRUE ORDER BY id ASC")
        products = cur.fetchall()
        return jsonify(products)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# --- ROTA 3: CALCULADORA DE ORÇAMENTO (O Cérebro) ---
@app.route('/api/checkout/calc', methods=['POST'])
def calculate_price():
    data = request.json
    product_id = data.get('product_id')
    selected_addons = data.get('addons_ids', []) # Lista de IDs ex: [1, 3]

    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro no banco"}), 500

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # 1. Pega preço do Produto Base
        cur.execute("SELECT name, price_setup, price_monthly FROM products WHERE id = %s", (product_id,))
        product = cur.fetchone()
        
        if not product:
            return jsonify({"error": "Produto não encontrado"}), 404

        total_setup = float(product['price_setup'])
        total_monthly = float(product['price_monthly'])
        items_summary = [f"{product['name']} (Base)"]

        # 2. Soma os Adicionais (se houver)
        if selected_addons:
            # Transforma lista [1, 2] em string "(1, 2)" para o SQL
            addons_tuple = tuple(selected_addons)
            if len(addons_tuple) == 1: addons_tuple = f"({addons_tuple[0]})" # Corrige tupla de 1 item
            
            query = f"SELECT name, price_setup, price_monthly FROM addons WHERE id IN {addons_tuple}"
            cur.execute(query)
            addons = cur.fetchall()

            for addon in addons:
                total_setup += float(addon['price_setup'])
                total_monthly += float(addon['price_monthly'])
                items_summary.append(f"+ {addon['name']}")

        return jsonify({
            "product_name": product['name'],
            "total_setup": round(total_setup, 2),
            "total_monthly": round(total_monthly, 2),
            "summary": items_summary
        })

    except Exception as e:
        print(f"Erro Calc: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# --- ROTA 4: WEBHOOK (Para Futuro) ---
@app.route('/webhooks/evolution', methods=['POST'])
def webhook():
    print("📩 Webhook recebido:", request.json)
    return jsonify({"status": "received"}), 200

if __name__ == '__main__':
    # Roda na porta 5000
    app.run(host='0.0.0.0', port=5000)