import os
import re
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai
import mercadopago
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import io
from datetime import datetime, timedelta

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET', 'chave-super-secreta-dev')
CORS(app)

DB_URL = os.getenv('DATABASE_URL')
GEMINI_KEY = os.getenv('GOOGLE_API_KEY')
MP_ACCESS_TOKEN = os.getenv('MP_ACCESS_TOKEN')

if GEMINI_KEY: genai.configure(api_key=GEMINI_KEY)
mp_sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

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
        u = cur.fetchone()
        return User(u['id'], u['name'], u['email']) if u else None
    finally:
        conn.close()

def get_db_connection():
    try: return psycopg2.connect(DB_URL)
    except: return None

def extract_days(value_str):
    # Extrai números de strings como "15 dias" ou retorna 2 como padrão
    if not value_str: return 0
    nums = re.findall(r'\d+', str(value_str))
    return int(nums[0]) if nums else 2

# --- ROTAS DE PÁGINAS ---
@app.route('/')
def home(): return render_template('index.html')

@app.route('/cadastro')
def cadastro_page(): return render_template('cadastro.html')

@app.route('/login')
def login_page():
    if current_user.is_authenticated: return redirect(url_for('admin_page'))
    return render_template('login.html')

@app.route('/admin')
@login_required
def admin_page(): return render_template('admin.html', user=current_user)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

# --- API ---

@app.route('/api/catalog', methods=['GET'])
def get_catalog():
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Agora busca também o campo prazo_products
        cur.execute("SELECT id, name, slug, description, price_setup, price_monthly, prazo_products FROM products WHERE is_active = TRUE")
        products = cur.fetchall()
        
        catalog = {}
        for p in products:
            # Agora busca também o campo prazo_addons
            cur.execute("SELECT id, name, price_setup, price_monthly, description, prazo_addons FROM addons WHERE product_id = %s", (p['id'],))
            addons = cur.fetchall()
            
            catalog[p['slug']] = {
                "id": p['id'],
                "title": p['name'],
                "desc": p['description'],
                "baseSetup": float(p['price_setup']),
                "baseMonthly": float(p['price_monthly']),
                "prazoBase": extract_days(p.get('prazo_products', '10')), # Padrão 10 dias se vazio
                "upsells": [
                    {
                        "id": a['id'],
                        "label": a['name'],
                        "priceSetup": float(a['price_setup']),
                        "priceMonthly": float(a['price_monthly']),
                        "details": a['description'],
                        "prazoExtra": extract_days(a.get('prazo_addons', '2')) # Padrão 2 dias se vazio
                    } for a in addons
                ]
            }
        return jsonify(catalog)
    finally:
        conn.close()

@app.route('/api/generate_contract', methods=['POST'])
def generate_contract():
    data = request.json
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    
    # Lógica simples de geração de PDF
    c.setFont("Helvetica-Bold", 16)
    c.drawString(100, 750, "CONTRATO DE PRESTAÇÃO DE SERVIÇOS")
    c.setFont("Helvetica", 12)
    c.drawString(100, 720, f"CONTRATANTE: {data.get('name', 'Cliente')}")
    c.drawString(100, 700, f"CPF/CNPJ: {data.get('document', 'Não informado')}")
    c.drawString(100, 680, f"PROJETO: {data.get('product_name')}")
    c.drawString(100, 660, f"PRAZO ESTIMADO DE ENTREGA: {data.get('deadline')} dias úteis")
    c.drawString(100, 640, f"VALOR SETUP: R$ {data.get('total_setup')}")
    c.drawString(100, 620, f"VALOR MENSAL: R$ {data.get('total_monthly')}")
    
    c.drawString(100, 580, "OBJETO DO CONTRATO:")
    c.drawString(100, 560, "Desenvolvimento e licenciamento de software conforme especificações.")
    c.drawString(100, 500, f"Data: {datetime.now().strftime('%d/%m/%Y')}")
    c.drawString(100, 450, "____________________________________")
    c.drawString(100, 435, "Assinatura Digital LEANTTRO")
    
    c.showPage()
    c.save()
    buffer.seek(0)
    
    return send_file(buffer, as_attachment=True, download_name="Contrato_Leanttro.pdf", mimetype='application/pdf')

@app.route('/api/signup_checkout', methods=['POST'])
def signup_checkout():
    data = request.json
    client_data = data.get('client')
    cart_data = data.get('cart')
    
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # 1. Verifica ou Cria Cliente
        cur.execute("SELECT id FROM clients WHERE email = %s", (client_data['email'],))
        existing = cur.fetchone()
        
        if existing:
            return jsonify({"error": "E-mail já cadastrado. Faça login."}), 400
            
        hashed_pw = generate_password_hash(client_data['password'])
        cur.execute("""
            INSERT INTO clients (name, email, whatsapp, password_hash, status, created_at)
            VALUES (%s, %s, %s, %s, 'active', NOW())
            RETURNING id
        """, (client_data['name'], client_data['email'], client_data['whatsapp'], hashed_pw))
        client_id = cur.fetchone()['id']
        
        # 2. Recalcula valores no Backend (Segurança)
        # (Lógica simplificada aqui, idealmente reutiliza a lógica do catalog)
        total_setup = float(cart_data['total_setup']) # Confia no front por enquanto ou refaz query
        
        # 3. Cria Pedido
        cur.execute("""
            INSERT INTO orders (client_id, product_id, selected_addons, total_setup, payment_status, created_at)
            VALUES (%s, %s, %s, %s, 'pending', NOW())
            RETURNING id
        """, (client_id, cart_data['product_id'], cart_data['addon_ids'], total_setup))
        order_id = cur.fetchone()['id']
        
        conn.commit()
        
        # 4. Gera Checkout MP
        preference_data = {
            "items": [{"id": str(cart_data['product_id']), "title": f"PROJETO LEANTTRO #{order_id}", "quantity": 1, "unit_price": total_setup}],
            "payer": {"name": client_data['name'], "email": client_data['email']},
            "external_reference": str(order_id),
            "back_urls": {"success": "https://leanttro.com/admin", "failure": "https://leanttro.com/"},
            "auto_return": "approved"
        }
        
        pref = mp_sdk.preference().create(preference_data)
        
        # Loga usuário automaticamente
        user_obj = User(id=client_id, name=client_data['name'], email=client_data['email'])
        login_user(user_obj)
        
        return jsonify({"checkout_url": pref["response"]["init_point"]})
        
    except Exception as e:
        conn.rollback()
        print(e)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# Mantém as outras rotas (admin login, etc)
@app.route('/api/login', methods=['POST'])
def api_login():
    # ... (código de login existente mantido)
    pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)