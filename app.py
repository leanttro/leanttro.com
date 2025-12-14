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
import traceback

# Importações para PDF
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET', 'chave-super-secreta-dev')
CORS(app)

# --- CONFIG UPLOAD ---
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf', 'doc', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- CONFIGURAÇÕES DO AMBIENTE ---
DB_URL = os.getenv('DATABASE_URL')
MP_ACCESS_TOKEN = os.getenv('MP_ACCESS_TOKEN')

# Tenta pegar a chave com os dois nomes possíveis pra não ter erro
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')

mp_sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# --- CONFIGURAÇÃO DO GEMINI (IGUAL AO COPIA.PY) ---
chat_model = None

try:
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        
        # --- PERSONA LELIS (VENDEDOR) ---
        SYSTEM_PROMPT_LELIS = """
        VOCÊ É: Lelis, Gerente Comercial da Leanttro Digital.
        SUA MISSÃO: Vender sites e sistemas. Você não é suporte técnico, você é FECHADOR de negócios.
        
        SEU TOM DE VOZ:
        - Profissional, mas agressivo em vendas ("Lobo de Wall Street").
        - Use gatilhos mentais de escassez ("Tenho poucas vagas na agenda").
        - Direto ao ponto. Respostas curtas (máximo 2 parágrafos).
        
        SEUS PRODUTOS E PREÇOS:
        1. Site Institucional (Ouro): Promoção de R$ 1.200 por R$ 499.
        2. Loja Virtual (Diamante): R$ 999.
        3. Sistemas Custom: A partir de R$ 1.500.

        REGRAS DE RESPOSTA:
        1. Se perguntarem preço, dê o preço e pergunte: "Vamos fechar agora e garantir esse valor?"
        2. Se perguntarem sobre o Leandro, diga: "O Leandro é o gênio técnico, eu sou quem resolve seu problema de negócios."
        3. DIRECONAMENTO: Sempre tente levar o cliente para clicar no botão de WhatsApp.
        4. O cliente não quer papo furado, ele quer solução.
        """

        # Inicializa o modelo globalmente para performance (Igual ao Copia.py)
        # Usando flash para ser rápido
        chat_model = genai.GenerativeModel(
            'gemini-1.5-flash', 
            system_instruction=SYSTEM_PROMPT_LELIS
        )
        print("✅ [Gemini] Chatbot Lelis inicializado com sucesso.")
    else:
        print("❌ ERRO: GEMINI_API_KEY/GOOGLE_API_KEY não encontrada.")
except Exception as e:
    print(f"❌ Erro ao inicializar Gemini: {e}")
    chat_model = None


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
    stats = {"users": 0, "orders": 0, "revenue": 0.0, "status_projeto": "AGUARDANDO", "revisoes": 2}
    try:
        if conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            if current_user.role == 'admin':
                cur.execute("SELECT COUNT(*) as c FROM clients")
                stats["users"] = cur.fetchone()['c']
                cur.execute("SELECT COUNT(*) as c FROM orders")
                stats["orders"] = cur.fetchone()['c']
                cur.execute("SELECT COALESCE(SUM(total_setup), 0) FROM orders WHERE payment_status = 'approved'")
                stats["revenue"] = float(cur.fetchone()[0])
            
            cur.execute("SELECT status, revisoes_restantes FROM briefings WHERE client_id = %s", (current_user.id,))
            briefing = cur.fetchone()
            if briefing:
                stats["status_projeto"] = briefing['status'].upper().replace('_', ' ')
                stats["revisoes"] = briefing['revisoes_restantes']
            
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
                
                cur.execute("SELECT id FROM briefings WHERE client_id = %s", (user_data['id'],))
                has_briefing = cur.fetchone()
                redirect_url = "/admin" if has_briefing else "/briefing"

                return jsonify({"message": "Sucesso", "redirect": redirect_url})
        
        return jsonify({"error": "Credenciais inválidas"}), 401
    finally:
        conn.close()

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
    finally:
        conn.close()

@app.route('/api/generate_contract', methods=['POST'])
def generate_contract():
    try:
        data = request.json
        buffer = io.BytesIO()
        p = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        
        p.setFillColorRGB(0.05, 0.05, 0.05)
        p.rect(0, height - 100, width, 100, fill=1)
        p.setFillColorRGB(0.82, 1, 0)
        p.setFont("Helvetica-BoldOblique", 24)
        p.drawString(50, height - 60, "LEANTTRO. DIGITAL SOLUTIONS")
        
        p.setFillColorRGB(0, 0, 0)
        p.setFont("Helvetica-Bold", 18)
        p.drawString(50, height - 150, "CONTRATO DE PRESTAÇÃO DE SERVIÇOS")
        
        p.setFont("Helvetica", 12)
        y = height - 200
        p.drawString(50, y, f"CONTRATANTE: {data.get('name', 'N/A').upper()}")
        p.drawString(50, y-20, f"DOCUMENTO: {data.get('document', 'N/A')}")
        
        y -= 80
        p.setFont("Helvetica-Bold", 14)
        p.drawString(50, y, "ESCOPO E PRAZOS")
        p.setFont("Helvetica", 12)
        y -= 25
        p.drawString(50, y, f"Projeto: {data.get('product_name')}")
        p.drawString(50, y-20, f"Entrega Estimada: {data.get('deadline')} dias úteis")
        p.drawString(50, y-40, f"Setup: {data.get('total_setup')}")
        p.drawString(50, y-60, f"Mensal: {data.get('total_monthly')}")
        
        y -= 100
        p.setFont("Helvetica-Bold", 12)
        p.drawString(50, y, "CLÁUSULA DE ALTERAÇÕES E REVISÕES")
        p.setFont("Helvetica", 10)
        y -= 20
        p.drawString(50, y, "1. O CONTRATANTE tem direito a 02 (duas) rodadas completas de revisão.")
        y -= 15
        p.drawString(50, y, "2. Alterações extras serão cobradas a R$ 150,00/hora técnica.")
        
        p.showPage()
        p.save()
        buffer.seek(0)
        return send_file(buffer, as_attachment=True, download_name=f"Contrato.pdf", mimetype='application/pdf')
    except Exception as e:
        return jsonify({"error": "Erro PDF"}), 500

@app.route('/api/signup_checkout', methods=['POST'])
def signup_checkout():
    if not mp_sdk: return jsonify({"error": "Mercado Pago Offline"}), 500
    
    data = request.json
    client = data.get('client')
    cart = data.get('cart')
    
    if not client or not cart: return jsonify({"error": "Dados incompletos"}), 400

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
        
        addons_ids = cart.get('addon_ids', [])
        addons_json = json.dumps(addons_ids) 
        
        total_setup = float(cart.get('total_setup') or 0)
        total_monthly = float(cart.get('total_monthly') or 0)
        
        cur.execute("""
            INSERT INTO orders (client_id, product_id, selected_addons, total_setup, total_monthly, payment_status, created_at)
            VALUES (%s, %s, %s, %s, %s, 'pending', NOW())
            RETURNING id
        """, (client_id, cart['product_id'], addons_json, total_setup, total_monthly))
        order_id = cur.fetchone()['id']
        
        conn.commit()
        
        preference_data = {
            "items": [{"id": str(cart['product_id']), "title": f"PROJETO WEB #{order_id}", "quantity": 1, "unit_price": total_setup}],
            "payer": {"name": client['name'], "email": client['email']},
            "external_reference": str(order_id),
            "back_urls": {
                "success": "https://leanttro.com/login", 
                "failure": "https://leanttro.com/cadastro",
                "pending": "https://leanttro.com/cadastro"
            },
            "auto_return": "approved"
        }
        
        pref = mp_sdk.preference().create(preference_data)
        login_user(User(client_id, client['name'], client['email']))
        
        return jsonify({"checkout_url": pref["response"]["init_point"]})

    except Exception as e:
        conn.rollback()
        print(f"Erro Signup Checkout: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

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

        tech_prompt_input = f"""
        ATUE COMO ARQUITETO DE SOFTWARE. Crie um prompt técnico:
        - CLIENTE: {current_user.name}
        - CORES: {colors}
        - ESTILO: {style}
        - SEÇÕES: {sections}
        - STACK: HTML, TailwindCSS, JS.
        - OUTPUT: Apenas o prompt técnico.
        """
        
        try:
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = model.generate_content(tech_prompt_input)
            tech_prompt = response.text
        except:
            tech_prompt = "Erro ao gerar com IA."

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
        return jsonify({"error": str(e)}), 500

# --- CHATBOT LELIS (ESTRUTURA IGUAL AO COPIA.PY) ---
# Endpoint: /api/chat (Igual ao frontend antigo)
# Lógica: Recebe histórico e usa sessão global
@app.route('/api/chat', methods=['POST'])
def handle_chat():
    print("\n--- [LELIS VENDAS] Recebido trigger para /api/chat ---")
    
    if not chat_model:
        print("❌ ERRO: Chat Model não inicializado.")
        return jsonify({'error': 'Serviço de IA Offline.'}), 503

    try:
        data = request.json
        # Pega o HISTÓRICO igual ao Copia.py
        history = data.get('conversationHistory', [])
        
        gemini_history = []
        for message in history:
            role = 'user' if message['role'] == 'user' else 'model'
            gemini_history.append({'role': role, 'parts': [{'text': message['text']}]})
            
        # Inicia o chat com histórico
        chat_session = chat_model.start_chat(history=gemini_history)
        
        # Pega a última mensagem ou usa padrão
        user_message = history[-1]['text'] if history and history[-1]['role'] == 'user' else "Olá"

        print(f"ℹ️  [Lelis] Cliente disse: '{user_message}'")
        
        response = chat_session.send_message(
            user_message,
            generation_config=genai.types.GenerationConfig(temperature=0.7),
            safety_settings={
                 'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE',
                 'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'
            }
        )
        print(f"✅  [Lelis] Resposta gerada.")
        return jsonify({'reply': response.text})

    except genai.types.generation_types.StopCandidateException as stop_ex:
        print(f"❌ API BLOQUEOU a resposta por segurança: {stop_ex}")
        return jsonify({'reply': "Desculpe, não posso responder isso. Mas bora focar no seu projeto?"})
    
    except Exception as e:
        print(f"❌ ERRO CRÍTICO no endpoint /api/chat: {e}")
        traceback.print_exc()
        # Fallback de erro com a persona do Lelis
        return jsonify({'reply': "Opa! Minha conexão deu uma oscilada aqui (muita gente chamando). Me chama no WhatsApp ali em cima que é mais garantido? 🚀"}), 200

@app.route('/api/briefing/chat', methods=['POST'])
@login_required
def briefing_chat():
    data = request.json
    last_msg = data.get('message')
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(f"Você é LIA, especialista em Briefing. Ajude o cliente a definir o site. Cliente: {last_msg}")
        return jsonify({"reply": response.text})
    except:
        return jsonify({"reply": "Erro de conexão com a IA."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)