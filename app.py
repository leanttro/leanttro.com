import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai
import mercadopago
from dotenv import load_dotenv

# Carrega variáveis de ambiente
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET', 'chave-super-secreta-dev') # Necessário para Sessão
CORS(app)

# --- CONFIGURAÇÕES GLOBAIS ---
DB_URL = os.getenv('DATABASE_URL')
GEMINI_KEY = os.getenv('GOOGLE_API_KEY')
MP_ACCESS_TOKEN = os.getenv('MP_ACCESS_TOKEN')

# Configuração IA
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

# Configuração Mercado Pago
mp_sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# --- CONFIGURAÇÃO DE LOGIN (AUTH) ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

class User(UserMixin):
    def __init__(self, id, name, email):
        self.id = id
        self.name = name
        self.email = email

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    if not conn: return None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name, email FROM clients WHERE id = %s", (user_id,))
        user_data = cur.fetchone()
        if user_data:
            return User(id=user_data['id'], name=user_data['name'], email=user_data['email'])
        return None
    except Exception as e:
        print(f"Erro Auth: {e}")
        return None
    finally:
        conn.close()

# --- FUNÇÕES AUXILIARES ---
def get_db_connection():
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        print(f"❌ Erro DB: {e}")
        return None

# --- ROTAS DE PÁGINAS (FRONTEND) ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('admin_page'))
    return render_template('login.html')

@app.route('/admin')
@login_required
def admin_page():
    # Passa o usuário logado para o HTML
    return render_template('admin.html', user=current_user)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

# --- ROTAS DE API (BACKEND) ---

# 1. LOGIN
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão"}), 500

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Busca usuário pelo email
        cur.execute("SELECT id, name, email, password_hash FROM clients WHERE email = %s", (email,))
        user_data = cur.fetchone()

        if user_data and user_data['password_hash']:
            # Verifica a senha hashada
            if check_password_hash(user_data['password_hash'], password):
                user_obj = User(id=user_data['id'], name=user_data['name'], email=user_data['email'])
                login_user(user_obj)
                return jsonify({"message": "Login realizado!", "redirect": "/admin"})
            else:
                return jsonify({"error": "Senha incorreta"}), 401
        else:
            return jsonify({"error": "Usuário não encontrado"}), 404
    except Exception as e:
        print(e)
        return jsonify({"error": "Erro no servidor"}), 500
    finally:
        conn.close()

# 2. CATÁLOGO DINÂMICO (Para popular o index.html)
@app.route('/api/products/catalog', methods=['GET'])
def get_catalog():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro DB"}), 500
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Busca produtos ativos
        cur.execute("""
            SELECT id, name, slug, description, price_setup, price_monthly 
            FROM products WHERE is_active = TRUE
        """)
        products = cur.fetchall()
        
        catalog = {}
        for p in products:
            # Busca addons para este produto
            cur.execute("""
                SELECT id, name, price_setup, price_monthly, description 
                FROM addons WHERE product_id = %s
            """, (p['id'],))
            addons = cur.fetchall()
            
            # Monta estrutura JSON compatível com o frontend
            catalog[p['slug']] = {
                "id": p['id'], # Importante para o pedido
                "title": p['name'],
                "desc": p['description'],
                "baseSetup": float(p['price_setup']),
                "baseMonthly": float(p['price_monthly']),
                "upsells": [
                    {
                        "id": a['id'],
                        "label": a['name'],
                        "priceSetup": float(a['price_setup']),
                        "priceMonthly": float(a['price_monthly']),
                        "details": a['description']
                    } for a in addons
                ]
            }
            
        return jsonify(catalog)
    except Exception as e:
        print(f"Erro Catalogo: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# 3. CHECKOUT SEGURO (Cria Order + Link MP)
@app.route('/api/checkout/create', methods=['POST'])
def create_checkout():
    if not mp_sdk: return jsonify({"error": "Mercado Pago offline"}), 500

    data = request.json
    # Dados do formulário do modal
    client_info = data.get('client')  # { name, email, whatsapp }
    cart = data.get('cart')           # { product_id, addon_ids: [] }

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # A. IDENTIFICAR OU CRIAR CLIENTE
        cur.execute("SELECT id FROM clients WHERE email = %s", (client_info['email'],))
        existing_client = cur.fetchone()
        
        if existing_client:
            client_id = existing_client['id']
        else:
            # Cria lead se não existe (Senha será definida depois ou enviada por email)
            # Dica: Você pode gerar uma senha temp aqui se quiser
            cur.execute("""
                INSERT INTO clients (name, email, whatsapp, status, created_at)
                VALUES (%s, %s, %s, 'lead', NOW())
                RETURNING id
            """, (client_info['name'], client_info['email'], client_info['whatsapp']))
            client_id = cur.fetchone()['id']
            conn.commit()

        # B. CALCULAR PREÇO NO SERVER (Segurança)
        # 1. Preço Produto Base
        cur.execute("SELECT price_setup, name FROM products WHERE id = %s", (cart['product_id'],))
        product_row = cur.fetchone()
        if not product_row: return jsonify({"error": "Produto inválido"}), 400
        
        total_setup = float(product_row['price_setup'])
        product_title = product_row['name']
        
        # 2. Preço Addons
        selected_addons_ids = cart.get('addon_ids', [])
        if selected_addons_ids:
            # Formata query segura para lista de IDs
            query_addons = "SELECT price_setup FROM addons WHERE id = ANY(%s)"
            cur.execute(query_addons, (selected_addons_ids,))
            addons_rows = cur.fetchall()
            for addon in addons_rows:
                total_setup += float(addon['price_setup'])

        # C. CRIAR PEDIDO (ORDER)
        cur.execute("""
            INSERT INTO orders (client_id, product_id, selected_addons, total_setup, payment_status, created_at)
            VALUES (%s, %s, %s, %s, 'pending', NOW())
            RETURNING id
        """, (client_id, cart['product_id'], selected_addons_ids, total_setup))
        order_id = cur.fetchone()['id']
        conn.commit()

        # D. GERAR LINK MERCADO PAGO
        preference_data = {
            "items": [
                {
                    "id": str(cart['product_id']),
                    "title": f"PROJETO: {product_title}",
                    "quantity": 1,
                    "unit_price": total_setup
                }
            ],
            "payer": {
                "name": client_info['name'],
                "email": client_info['email']
            },
            "external_reference": str(order_id), # VINCULA O PAGAMENTO AO PEDIDO
            "back_urls": {
                "success": "https://leanttro.com/admin", 
                "failure": "https://leanttro.com/",
                "pending": "https://leanttro.com/"
            },
            "auto_return": "approved"
        }
        
        pref_response = mp_sdk.preference().create(preference_data)
        payment_url = pref_response["response"]["init_point"]
        
        return jsonify({"checkout_url": payment_url})

    except Exception as e:
        conn.rollback()
        print(f"Erro Checkout: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# 4. CHATBOT (RAG)
@app.route('/api/chat/message', methods=['POST'])
def chat_message():
    # ... (Mantenha seu código de chat aqui, pode usar o RAG na tabela 'leanttro_rag_knowledge' depois)
    return jsonify({"reply": "Estou em manutenção para upgrade de segurança. Volto logo!"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)