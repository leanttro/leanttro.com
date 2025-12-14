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
from werkzeug.utils import secure_filename
import google.generativeai as genai
import mercadopago
from dotenv import load_dotenv

# Importações para PDF
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET', 'chave-super-secreta-dev')
CORS(app)

# CONFIGURAÇÃO DE UPLOAD
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf', 'doc', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- CONFIGURAÇÕES DE AMBIENTE ---
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
        cur.execute("SELECT id, name, email, status FROM clients WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if u:
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
    if not value: return 0
    if isinstance(value, int): return value
    nums = re.findall(r'\d+', str(value))
    return int(nums[0]) if nums else 0

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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
        # LÓGICA DE REDIRECIONAMENTO INTELIGENTE
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM briefings WHERE client_id = %s", (current_user.id,))
            if cur.fetchone():
                return redirect(url_for('admin_page'))
            else:
                return redirect(url_for('briefing_page'))
        finally:
            conn.close()
    return render_template('login.html')

@app.route('/briefing')
@login_required
def briefing_page():
    # Segurança: Se já tem briefing, não deixa acessar essa página de novo
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM briefings WHERE client_id = %s", (current_user.id,))
        if cur.fetchone():
            return redirect(url_for('admin_page'))
    finally:
        conn.close()
    return render_template('briefing.html', user=current_user)

@app.route('/admin')
@login_required
def admin_page():
    conn = get_db_connection()
    stats = {"users": 0, "orders": 0, "revenue": 0.0, "revisoes": 2, "status_projeto": "AGUARDANDO BRIEFING"}
    
    try:
        if conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            # Dados Gerais (Admin View)
            if current_user.role == 'admin':
                cur.execute("SELECT COUNT(*) as c FROM clients")
                stats["users"] = cur.fetchone()['c']
                cur.execute("SELECT COUNT(*) as c FROM orders")
                stats["orders"] = cur.fetchone()['c']
            
            # Dados do Cliente Logado (User View)
            cur.execute("SELECT status, revisoes_restantes FROM briefings WHERE client_id = %s", (current_user.id,))
            briefing = cur.fetchone()
            if briefing:
                stats["status_projeto"] = briefing['status'].upper().replace('_', ' ')
                stats["revisoes"] = briefing['revisoes_restantes']
            
            conn.close()
    except Exception as e:
        print(f"Erro Admin Stats: {e}")
        
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
                
                # Verifica briefing para redirecionar corretamente
                cur.execute("SELECT id FROM briefings WHERE client_id = %s", (user_data['id'],))
                has_briefing = cur.fetchone()
                redirect_url = "/admin" if has_briefing else "/briefing"
                
                return jsonify({"message": "Sucesso", "redirect": redirect_url})
        
        return jsonify({"error": "Credenciais inválidas"}), 401
    finally:
        conn.close()

# 2. CATÁLOGO
@app.route('/api/catalog', methods=['GET'])
def get_catalog():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de Conexão"}), 500
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name, slug, description, price_setup, price_monthly, prazo_products FROM products WHERE is_active = TRUE")
        products = cur.fetchall()
        
        catalog = {}
        for p in products:
            cur.execute("SELECT id, name, price_setup, price_monthly, description, prazo_addons FROM addons WHERE product_id = %s", (p['id'],))
            addons = cur.fetchall()
            
            catalog[p['slug']] = {
                "id": p['id'],
                "title": p['name'],
                "desc": p['description'],
                "baseSetup": float(p['price_setup']),
                "baseMonthly": float(p['price_monthly']),
                "prazoBase": extract_days(p.get('prazo_products')) or 10,
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
    finally:
        conn.close()

# 3. GERAÇÃO DE CONTRATO (ATUALIZADO COM REVISÕES)
@app.route('/api/generate_contract', methods=['POST'])
def generate_contract():
    try:
        data = request.json
        buffer = io.BytesIO()
        p = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        
        # Header Neon Style
        p.setFillColorRGB(0.05, 0.05, 0.05)
        p.rect(0, height - 100, width, 100, fill=1)
        p.setFillColorRGB(0.82, 1, 0)
        p.setFont("Helvetica-BoldOblique", 24)
        p.drawString(50, height - 60, "LEANTTRO. DIGITAL SOLUTIONS")
        
        # Corpo
        p.setFillColorRGB(0, 0, 0)
        p.setFont("Helvetica-Bold", 18)
        p.drawString(50, height - 150, "CONTRATO DE PRESTAÇÃO DE SERVIÇOS")
        
        p.setFont("Helvetica", 12)
        y = height - 200
        p.drawString(50, y, f"CONTRATANTE: {data.get('name', 'N/A').upper()}")
        p.drawString(50, y-20, f"DOCUMENTO: {data.get('document', 'N/A')}")
        p.drawString(50, y-40, f"DATA: {datetime.now().strftime('%d/%m/%Y')}")
        
        y -= 80
        p.setFont("Helvetica-Bold", 14)
        p.drawString(50, y, "ESCOPO DO PROJETO")
        p.setFont("Helvetica", 12)
        y -= 25
        p.drawString(50, y, f"Serviço: {data.get('product_name')}")
        p.drawString(50, y-20, f"Entrega Estimada: {data.get('deadline')} dias úteis")
        p.drawString(50, y-40, f"Valor Setup: {data.get('total_setup')}")
        p.drawString(50, y-60, f"Valor Mensal: {data.get('total_monthly')}")

        # CLÁUSULA DE REVISÕES (NOVO)
        y -= 100
        p.setFont("Helvetica-Bold", 12)
        p.drawString(50, y, "CLÁUSULA DE ALTERAÇÕES E REVISÕES:")
        p.setFont("Helvetica", 10)
        y -= 20
        p.drawString(50, y, "1. O CONTRATANTE tem direito a 02 (duas) rodadas completas de revisão após a entrega da V1.")
        y -= 15
        p.drawString(50, y, "2. Alterações adicionais ou mudanças de escopo serão cobradas a R$ 150,00/hora técnica.")
        
        p.showPage()
        p.save()
        buffer.seek(0)
        return send_file(buffer, as_attachment=True, download_name="Contrato_Leanttro.pdf", mimetype='application/pdf')
    except Exception as e:
        return jsonify({"error": "Erro ao gerar PDF"}), 500

# 4. SIGNUP + CHECKOUT
@app.route('/api/signup_checkout', methods=['POST'])
def signup_checkout():
    if not mp_sdk: return jsonify({"error": "Mercado Pago Offline"}), 500
    
    data = request.json
    client = data.get('client')
    cart = data.get('cart')
    
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cur.execute("SELECT id FROM clients WHERE email = %s", (client['email'],))
        existing = cur.fetchone()
        
        if existing:
            client_id = existing['id']
        else:
            hashed = generate_password_hash(client['password'])
            cur.execute("""
                INSERT INTO clients (name, email, whatsapp, password_hash, status, created_at)
                VALUES (%s, %s, %s, %s, 'active', NOW())
                RETURNING id
            """, (client['name'], client['email'], client['whatsapp'], hashed))
            client_id = cur.fetchone()['id']
        
        # Salva valores
        addons_ids = cart.get('addon_ids', [])
        total_setup = float(cart.get('total_setup', 0))
        total_monthly = float(cart.get('total_monthly', 0))
        
        cur.execute("""
            INSERT INTO orders (client_id, product_id, selected_addons, total_setup, total_monthly, payment_status, created_at)
            VALUES (%s, %s, %s, %s, %s, 'pending', NOW())
            RETURNING id
        """, (client_id, cart['product_id'], addons_ids, total_setup, total_monthly))
        order_id = cur.fetchone()['id']
        
        conn.commit()
        
        preference_data = {
            "items": [{"id": str(cart['product_id']), "title": f"PROJETO WEB #{order_id}", "quantity": 1, "unit_price": total_setup}],
            "payer": {"name": client['name'], "email": client['email']},
            "external_reference": str(order_id),
            "back_urls": {
                "success": "https://leanttro.com/login", # Manda pro login para cair no briefing
                "failure": "https://leanttro.com/cadastro",
                "pending": "https://leanttro.com/cadastro"
            },
            "auto_return": "approved"
        }
        
        pref = mp_sdk.preference().create(preference_data)
        
        # Loga usuário
        login_user(User(client_id, client['name'], client['email']))
        
        return jsonify({"checkout_url": pref["response"]["init_point"]})
    finally:
        conn.close()

# 5. SALVAR BRIEFING + GERAR PROMPT TÉCNICO (NOVO)
@app.route('/api/briefing/save', methods=['POST'])
@login_required
def save_briefing():
    try:
        colors = request.form.get('colors')
        style = request.form.get('style')
        sections = request.form.get('sections')
        
        file_names = []
        if 'files' in request.files:
            files = request.files.getlist('files')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(f"{current_user.id}_{file.filename}")
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    file_names.append(filename)

        # Prompt para a IA agir como Senior Developer e criar a spec técnica
        tech_prompt_input = f"""
        ACT AS A SENIOR FRONTEND ARCHITECT.
        CREATE A DETAILED TECHNICAL PROMPT FOR A DEVELOPER TO BUILD A WEBSITE WITH THESE SPECS:
        - CLIENT: {current_user.name}
        - PREFERRED COLORS: {colors}
        - STYLE: {style}
        - SECTIONS: {sections}
        - STACK: HTML5, TAILWINDCSS, JS (NO FRAMEWORKS LIKE REACT).
        - OUTPUT: JUST THE TECHNICAL PROMPT.
        """
        
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(tech_prompt_input)
        tech_prompt = response.text

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO briefings (client_id, colors, style_preference, site_sections, uploaded_files, ai_generated_prompt, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pendente')
        """, (current_user.id, colors, style, sections, ",".join(file_names), tech_prompt))
        conn.commit()
        conn.close()

        return jsonify({"success": True, "redirect": "/admin"})
    except Exception as e:
        print(e)
        return jsonify({"error": str(e)}), 500

# 6. CHATBOT RAG / BRIEFING (Restaurado e Expandido)
@app.route('/api/chat/message', methods=['POST']) # Rota genérica do site
def chat_message():
    # Aqui você pode colar sua lógica de RAG antiga se tiver backup, 
    # ou usar esta versão simplificada com Gemini que responde dúvidas comerciais
    data = request.json
    msg = data.get('message')
    
    try:
        model = genai.GenerativeModel('gemini-pro')
        chat = model.start_chat(history=[])
        response = chat.send_message(f"Você é um assistente comercial da Leanttro. Responda curto e vendedor: {msg}")
        return jsonify({"reply": response.text})
    except:
        return jsonify({"reply": "Estou em manutenção, fale no WhatsApp!"})

@app.route('/api/briefing/chat', methods=['POST']) # Rota nova do Briefing
@login_required
def briefing_chat():
    data = request.json
    history = data.get('history', [])
    last_msg = data.get('message')

    system = "Você é LIA, especialista em Briefing. Entreviste o cliente sobre Cores, Estilo e Seções do site. Seja breve."
    
    try:
        model = genai.GenerativeModel('gemini-pro')
        # Converte histórico simples para formato do Gemini se necessário, ou manda tudo no prompt
        full_prompt = f"{system}\nHistórico: {history}\nCliente: {last_msg}"
        response = model.generate_content(full_prompt)
        return jsonify({"reply": response.text})
    except Exception as e:
        return jsonify({"reply": "Erro de conexão com a IA."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)