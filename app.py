import os
import re
import io
import json
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, session
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai
import mercadopago
from dotenv import load_dotenv

# Importações para PDF
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET', 'chave-super-secreta-dev')
CORS(app)

# --- CONFIGURAÇÕES ---
DB_URL = os.getenv('DATABASE_URL')
GEMINI_KEY = os.getenv('GOOGLE_API_KEY')
MP_ACCESS_TOKEN = os.getenv('MP_ACCESS_TOKEN')

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

mp_sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# --- AUTHENTICATION SETUP ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

class User(UserMixin):
    def __init__(self, id, name, email, role='user'):
        self.id = id
        self.name = name
        self.email = email
        self.role = role

def get_db_connection():
    try:
        return psycopg2.connect(DB_URL)
    except Exception as e:
        print(f"❌ Erro de Conexão DB: {e}")
        return None

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    if not conn: return None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Tenta achar em clients (usuários comuns e admins)
        cur.execute("SELECT id, name, email, status FROM clients WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if u:
            # Se tiver status 'admin', define role como admin, senão user
            role = 'admin' if u.get('status') == 'admin' else 'user'
            return User(u['id'], u['name'], u['email'], role)
        return None
    except Exception as e:
        print(f"Erro Auth Loader: {e}")
        return None
    finally:
        conn.close()

# --- HELPER FUNCTIONS ---
def extract_days(value):
    """Extrai número de dias de strings ou retorna padrão"""
    if not value: return 0
    # Se já for int, retorna
    if isinstance(value, int): return value
    # Se for string "15 dias", extrai 15
    nums = re.findall(r'\d+', str(value))
    return int(nums[0]) if nums else 0

# --- ROTAS DE FRONTEND ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/cadastro')
def cadastro_page():
    return render_template('cadastro.html')

@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('admin_page'))
    return render_template('login.html')

@app.route('/admin')
@login_required
def admin_page():
    # Proteção básica: redireciona se não for admin (opcional, dependendo da sua regra de negócio)
    # if current_user.role != 'admin': return redirect(url_for('home'))
    
    # Busca estatísticas para o dashboard
    conn = get_db_connection()
    stats = {"users": 0, "orders": 0, "revenue": 0.0}
    try:
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM clients")
            stats["users"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM orders")
            stats["orders"] = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(total_setup), 0) FROM orders WHERE payment_status = 'approved'")
            stats["revenue"] = float(cur.fetchone()[0])
            conn.close()
    except:
        pass
        
    return render_template('admin.html', user=current_user, stats=stats)

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
    if not conn: return jsonify({"error": "Erro DB"}), 500

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name, email, password_hash, status FROM clients WHERE email = %s", (email,))
        user_data = cur.fetchone()

        if user_data and user_data['password_hash']:
            if check_password_hash(user_data['password_hash'], password):
                user_obj = User(user_data['id'], user_data['name'], user_data['email'])
                login_user(user_obj)
                return jsonify({"message": "Sucesso", "redirect": "/admin"})
        
        return jsonify({"error": "Credenciais inválidas"}), 401
    finally:
        conn.close()

# 2. CATÁLOGO (CORRIGIDO E ATUALIZADO COM PRAZOS)
@app.route('/api/catalog', methods=['GET'])
def get_catalog():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de Conexão"}), 500
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Busca produtos ativos e o campo de prazo
        cur.execute("""
            SELECT id, name, slug, description, price_setup, price_monthly, prazo_products 
            FROM products 
            WHERE is_active = TRUE
        """)
        products = cur.fetchall()
        
        catalog = {}
        for p in products:
            # Busca addons e o campo de prazo
            cur.execute("""
                SELECT id, name, price_setup, price_monthly, description, prazo_addons 
                FROM addons 
                WHERE product_id = %s
            """, (p['id'],))
            addons = cur.fetchall()
            
            # Tratamento de erro se prazo for None (Padrão: 10 dias produtos, 2 dias addons)
            prazo_prod = extract_days(p.get('prazo_products')) or 10
            
            catalog[p['slug']] = {
                "id": p['id'],
                "title": p['name'],
                "desc": p['description'],
                "baseSetup": float(p['price_setup']),
                "baseMonthly": float(p['price_monthly']),
                "prazoBase": prazo_prod,
                "upsells": [
                    {
                        "id": a['id'],
                        "label": a['name'],
                        "priceSetup": float(a['price_setup']),
                        "priceMonthly": float(a['price_monthly']),
                        "details": a['description'],
                        "prazoExtra": extract_days(a.get('prazo_addons')) or 2
                    } for a in addons
                ]
            }
            
        return jsonify(catalog)
    except Exception as e:
        print(f"Erro Catalog: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# 3. GERAÇÃO DE CONTRATO (NOVO)
@app.route('/api/generate_contract', methods=['POST'])
def generate_contract():
    try:
        data = request.json
        buffer = io.BytesIO()
        p = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        
        # Cabeçalho
        p.setFillColorRGB(0.05, 0.05, 0.05) # Quase preto
        p.rect(0, height - 100, width, 100, fill=1)
        p.setFillColorRGB(0.82, 1, 0) # Neon
        p.setFont("Helvetica-BoldOblique", 24)
        p.drawString(50, height - 60, "LEANTTRO. DIGITAL SOLUTIONS")
        
        # Título
        p.setFillColorRGB(0, 0, 0)
        p.setFont("Helvetica-Bold", 18)
        p.drawString(50, height - 150, "CONTRATO DE PRESTAÇÃO DE SERVIÇOS")
        
        # Dados do Cliente
        p.setFont("Helvetica", 12)
        y = height - 200
        p.drawString(50, y, f"CONTRATANTE: {data.get('name', 'N/A').upper()}")
        p.drawString(50, y-20, f"DOCUMENTO (CPF/CNPJ): {data.get('document', 'N/A')}")
        p.drawString(50, y-40, f"DATA DA SOLICITAÇÃO: {datetime.now().strftime('%d/%m/%Y')}")
        
        # Objeto do Contrato
        y -= 80
        p.setFont("Helvetica-Bold", 14)
        p.drawString(50, y, "OBJETO DO CONTRATO")
        p.setFont("Helvetica", 12)
        y -= 25
        p.drawString(50, y, f"Desenvolvimento de Projeto Web: {data.get('product_name')}")
        y -= 20
        p.drawString(50, y, f"Prazo Estimado de Entrega: {data.get('deadline')} dias úteis (após recebimento do material)")
        
        # Valores
        y -= 60
        p.setFont("Helvetica-Bold", 14)
        p.drawString(50, y, "INVESTIMENTO")
        p.setFont("Helvetica", 12)
        y -= 25
        p.drawString(50, y, f"Setup (Criação): R$ {data.get('total_setup')}")
        y -= 20
        p.drawString(50, y, f"Manutenção Mensal: {data.get('total_monthly')}")
        
        # Termos Legais (Resumo)
        y -= 60
        p.setFont("Helvetica-Oblique", 10)
        p.drawString(50, y, "Este documento é uma minuta de solicitação. O contrato definitivo entra em vigor")
        p.drawString(50, y-15, "após a confirmação do pagamento da taxa de setup.")
        
        # Assinatura Digital
        y -= 80
        p.setStrokeColorRGB(0.8, 0.8, 0.8)
        p.line(50, y, 250, y)
        p.drawString(50, y-15, "Assinado Digitalmente por LEANTTRO")
        
        p.showPage()
        p.save()
        buffer.seek(0)
        
        return send_file(buffer, as_attachment=True, download_name=f"Contrato_Leanttro_{data.get('document')}.pdf", mimetype='application/pdf')
    except Exception as e:
        print(f"Erro PDF: {e}")
        return jsonify({"error": "Falha ao gerar contrato"}), 500

# 4. CHECKOUT COM CADASTRO (CORRIGIDO PARA SALVAR MENSALIDADE)
@app.route('/api/signup_checkout', methods=['POST'])
def signup_checkout():
    if not mp_sdk: return jsonify({"error": "Mercado Pago Offline"}), 500
    
    data = request.json
    client = data.get('client')
    cart = data.get('cart')
    
    if not client or not cart:
        return jsonify({"error": "Dados incompletos"}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # A. Verifica se usuário já existe
        cur.execute("SELECT id FROM clients WHERE email = %s", (client['email'],))
        existing = cur.fetchone()
        
        if existing:
            client_id = existing['id']
            # Opcional: Atualizar dados se necessário
        else:
            # B. Cria Usuário com Senha Hashada
            hashed = generate_password_hash(client['password'])
            cur.execute("""
                INSERT INTO clients (name, email, whatsapp, password_hash, status, created_at)
                VALUES (%s, %s, %s, %s, 'active', NOW())
                RETURNING id
            """, (client['name'], client['email'], client['whatsapp'], hashed))
            client_id = cur.fetchone()['id']
        
        # C. Cria o Pedido (Order)
        addons_ids = cart.get('addon_ids', [])
        total_setup = float(cart.get('total_setup', 0))
        total_monthly = float(cart.get('total_monthly', 0)) # CORREÇÃO: Lê o valor mensal
        
        cur.execute("""
            INSERT INTO orders (client_id, product_id, selected_addons, total_setup, total_monthly, payment_status, created_at)
            VALUES (%s, %s, %s, %s, %s, 'pending', NOW())
            RETURNING id
        """, (client_id, cart['product_id'], addons_ids, total_setup, total_monthly)) # CORREÇÃO: Salva no banco
        order_id = cur.fetchone()['id']
        
        conn.commit()
        
        # D. Gera Checkout Mercado Pago
        preference_data = {
            "items": [
                {
                    "id": str(cart['product_id']),
                    "title": f"PROJETO WEB #{order_id}",
                    "quantity": 1,
                    "unit_price": total_setup
                }
            ],
            "payer": {
                "name": client['name'],
                "email": client['email'],
                "identification": {
                    "type": "CPF/CNPJ",
                    "number": client['document']
                }
            },
            "external_reference": str(order_id),
            "back_urls": {
                "success": "https://leanttro.com/admin", 
                "failure": "https://leanttro.com/cadastro",
                "pending": "https://leanttro.com/cadastro"
            },
            "auto_return": "approved"
        }
        
        pref = mp_sdk.preference().create(preference_data)
        checkout_url = pref["response"]["init_point"]
        
        # Loga o usuário na sessão
        user_obj = User(client_id, client['name'], client['email'])
        login_user(user_obj)
        
        return jsonify({"checkout_url": checkout_url})

    except Exception as e:
        conn.rollback()
        print(f"Erro Signup Checkout: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# 5. CHATBOT (Mantido)
@app.route('/api/chat/message', methods=['POST'])
def chat_message():
    data = request.json
    msg = data.get('message', '')
    # Aqui entraria sua lógica RAG existente
    return jsonify({"reply": "Estou processando seu pedido de desenvolvimento."})

if __name__ == '__main__':
    # Threaded=True ajuda a evitar bloqueios em requests simultâneos
    app.run(host='0.0.0.0', port=5000, threaded=True)