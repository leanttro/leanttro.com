import os
import re
import io
import json
import psycopg2
import psycopg2.extras
from psycopg2 import pool
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, session, abort, send_from_directory
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import google.generativeai as genai
import mercadopago
from dotenv import load_dotenv
import traceback

# --- NOVAS IMPORTAÇÕES PARA EMAIL ---
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from itsdangerous import URLSafeTimedSerializer

# Importações para PDF
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

load_dotenv()

app = Flask(__name__, static_folder='static', static_url_path='/static') 

app.secret_key = os.getenv('SECRET', 'chave-super-secreta-dev')
CORS(app)

# --- CONFIG UPLOAD ---
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf', 'doc', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- CONFIGURAÇÕES ---
DB_URL = os.getenv('DATABASE_URL')
MP_ACCESS_TOKEN = os.getenv('MP_ACCESS_TOKEN')
DIRECTUS_ASSETS_URL = "https://api.leanttro.com/assets/"
COMPANY_CNPJ = "63.556.406/0001-75"
BASE_URL = os.getenv('APP_BASE_URL', 'https://leanttro.com') # Ajuste se necessário

# --- CONFIGURAÇÃO SMTP (EMAIL) ---
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))

# --- CONFIGURAÇÃO DB POOL ---
db_pool = None

# --- FUNÇÃO DE INICIALIZAÇÃO DO BANCO (AUTO-CORREÇÃO) ---
def init_db():
    """Garante que a tabela invoices exista, já que ela sumiu do Directus"""
    global db_pool
    try:
        if DB_URL:
            db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, DB_URL)
            print("✅ Pool de Conexões criado com sucesso")
            
            # --- AUTO-FIX: CRIA TABELA INVOICES SE NÃO EXISTIR ---
            conn = db_pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS invoices (
                        id SERIAL PRIMARY KEY,
                        client_id INTEGER,
                        amount DECIMAL(10,2) NOT NULL,
                        due_date DATE NOT NULL,
                        status VARCHAR(50) DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT NOW(),
                        paid_at TIMESTAMP
                    );
                """)
                # Garante coluna url_versao em briefings se não existir (para evitar erro no admin)
                cur.execute("ALTER TABLE briefings ADD COLUMN IF NOT EXISTS url_versao TEXT;")
                
                conn.commit()
                print("✅ [SISTEMA] Tabelas verificadas com sucesso.")
            except Exception as e:
                conn.rollback()
                print(f"❌ Erro ao verificar tabelas: {e}")
            finally:
                db_pool.putconn(conn)
        else:
            print("❌ DATABASE_URL não encontrada.")
    except Exception as e:
        print(f"❌ Erro ao criar Pool: {e}")

# INICIA O BANCO IMEDIATAMENTE
init_db()

# --- CONFIGURAÇÃO GEMINI (AUTO-DETECT) ---
GEMINI_KEY = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
chat_model = None
SELECTED_MODEL_NAME = "gemini-pro"

if GEMINI_KEY:
    try:
        genai.configure(api_key=GEMINI_KEY)
        
        print("\n========== [DIAGNÓSTICO DE MODELOS] ==========")
        found_model = None
        try:
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    clean_name = m.name.replace('models/', '')
                    if not found_model: found_model = clean_name
        except Exception as e_list:
            print(f"Erro listar modelos: {e_list}")

        if found_model:
            SELECTED_MODEL_NAME = found_model
            print(f"🎯 MODELO SELECIONADO: {SELECTED_MODEL_NAME}")

        SYSTEM_PROMPT_LELIS = """
        VOCÊ É: Lelis, Consultor Executivo da Leanttro Digital.
        SUA MISSÃO: Fechar contratos de alto valor passando autoridade e segurança técnica.
        TOM: Profissional, Seguro, Educado e Direto. Não use gírias.
        """
        
        try:
            chat_model = genai.GenerativeModel(
                SELECTED_MODEL_NAME,
                system_instruction=SYSTEM_PROMPT_LELIS
            )
        except:
            print("⚠️ Fallback: Iniciando Gemini sem System Prompt.")
            chat_model = genai.GenerativeModel(SELECTED_MODEL_NAME)
            
    except Exception as e:
        print(f"❌ Erro Gemini: {e}")
else:
    print("❌ Nenhuma API Key do Google encontrada.")


mp_sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# --- AUTHENTICATION SETUP ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

class User(UserMixin):
    def __init__(self, id, name, email, role='user', status='active'):
        self.id = id
        self.name = name
        self.email = email
        self.role = role
        self.status = status

# --- GET DB CONNECTION COM POOL ---
def get_db_connection():
    try:
        if db_pool:
            return db_pool.getconn()
        else:
            return psycopg2.connect(DB_URL) 
    except Exception as e:
        print(f"❌ Erro de Conexão DB: {e}")
        return None

# --- FUNÇÃO AUXILIAR: ENVIO DE EMAIL ---
def enviar_email(destinatario, link_recuperacao):
    """Envia e-mail de recuperação usando SMTP"""
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print("⚠️ SMTP não configurado nas variáveis de ambiente.")
        return False

    msg = MIMEMultipart()
    msg['From'] = SMTP_EMAIL
    msg['To'] = destinatario
    msg['Subject'] = "Recuperação de Senha - Leanttro"

    html = f"""
    <html>
      <body style="font-family: 'Courier New', monospace; background-color: #050505; color: #fff; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; border: 1px solid #333; padding: 30px;">
            <h2 style="color: #D2FF00; font-style: italic;">LEANTTRO.</h2>
            <p style="color: #ccc;">Olá,</p>
            <p style="color: #ccc;">Recebemos uma solicitação para redefinir sua senha de acesso.</p>
            <div style="text-align: center; margin: 40px 0;">
                <a href="{link_recuperacao}" style="background-color: #D2FF00; color: #000; padding: 15px 30px; text-decoration: none; font-weight: bold; font-style: italic; text-transform: uppercase;">DEFINIR NOVA SENHA</a>
            </div>
            <p style="font-size: 12px; color: #666;">Se você não solicitou isso, ignore este e-mail.</p>
        </div>
      </body>
    </html>
    """
    msg.attach(MIMEText(html, 'html'))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, destinatario, msg.as_string())
        server.quit()
        print(f"✅ E-mail enviado para {destinatario}")
        return True
    except Exception as e:
        print(f"❌ Erro ao enviar e-mail: {e}")
        return False

# --- FUNÇÃO: GARANTIR FATURAS FUTURAS ---
def ensure_future_invoices(client_id):
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                client_id INTEGER,
                amount DECIMAL(10,2) NOT NULL,
                due_date DATE NOT NULL,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                paid_at TIMESTAMP
            );
        """)
        
        monthly_price = 0.0
        cur.execute("""
            SELECT o.total_monthly, p.price_monthly, o.created_at as order_date 
            FROM orders o
            LEFT JOIN products p ON CAST(o.product_id AS VARCHAR) = CAST(p.id AS VARCHAR)
            WHERE o.client_id = %s 
            ORDER BY o.id ASC
        """, (client_id,))
        orders = cur.fetchall()
        
        first_order_date = date.today()
        for o in orders:
            val_order = float(o.get('total_monthly') or 0)
            val_product = float(o.get('price_monthly') or 0)
            if o.get('order_date'): first_order_date = o['order_date'].date()
            if val_order > 0:
                monthly_price = val_order
                break
            elif val_product > 0:
                monthly_price = val_product
                break
        
        if monthly_price <= 0:
            cur.execute("SELECT price_monthly FROM products WHERE is_active = TRUE AND price_monthly > 0 LIMIT 1")
            default_prod = cur.fetchone()
            if default_prod: monthly_price = float(default_prod['price_monthly'])

        if monthly_price <= 0: return

        cur.execute("SELECT COUNT(*) as c FROM invoices WHERE client_id = %s AND status = 'pending'", (client_id,))
        count_pending = cur.fetchone()['c']
        needed = 12 - count_pending
        
        if needed > 0:
            cur.execute("SELECT due_date FROM invoices WHERE client_id = %s ORDER BY due_date DESC LIMIT 1", (client_id,))
            last_inv = cur.fetchone()
            
            if last_inv:
                last_date = last_inv['due_date']
                start_month = last_date.month
                start_year = last_date.year
            else:
                free_until = first_order_date + timedelta(days=30)
                target_due_date = date(free_until.year, free_until.month, 10)
                if target_due_date < free_until:
                    if target_due_date.month == 12: target_due_date = date(target_due_date.year + 1, 1, 10)
                    else: target_due_date = date(target_due_date.year, target_due_date.month + 1, 10)
                start_month = target_due_date.month - 1
                start_year = target_due_date.year
                if start_month == 0:
                    start_month = 12
                    start_year -= 1
            
            for i in range(1, needed + 1):
                calc_month = start_month + i
                year_offset = (calc_month - 1) // 12
                final_month = (calc_month - 1) % 12 + 1
                final_year = start_year + year_offset
                due_dt = date(final_year, final_month, 10)
                
                cur.execute("SELECT id FROM invoices WHERE client_id = %s AND due_date = %s", (client_id, due_dt))
                if not cur.fetchone():
                    cur.execute("INSERT INTO invoices (client_id, amount, due_date, status) VALUES (%s, %s, %s, 'pending')", (client_id, monthly_price, due_dt))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Erro Faturas: {e}")
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

# --- FUNÇÃO: DASHBOARD FINANCEIRO ---
def get_financial_dashboard(client_id):
    ensure_future_invoices(client_id)
    conn = get_db_connection()
    info = {
        "status_global": "ok", "message": "EM DIA", "invoices": [],
        "total_pending": 0.0, "total_annual_discounted": 0.0, "annual_savings": 0.0
    }
    if not conn: return info

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, amount, due_date, status FROM invoices WHERE client_id = %s AND status = 'pending' ORDER BY due_date ASC", (client_id,))
        raw_invoices = cur.fetchall()
        today = date.today()
        
        for inv in raw_invoices:
            due = inv['due_date']
            delta = (today - due).days
            inv_data = {
                "id": inv['id'], "amount": float(inv['amount']),
                "date_fmt": due.strftime('%d/%m/%Y'), "status_label": "A VENCER", "class": "text-white"
            }
            if delta > 3:
                info["status_global"] = "overdue"
                info["message"] = "BLOQUEADO (FATURA ATRASADA)"
                inv_data["status_label"] = "ATRASADO"
                inv_data["class"] = "text-red-500 font-bold"
            elif delta >= 0:
                if info["status_global"] != "overdue": info["status_global"] = "warning"
                if info["message"] != "BLOQUEADO (FATURA ATRASADA)": info["message"] = "VENCE HOJE"
                inv_data["status_label"] = "VENCE HOJE"
                inv_data["class"] = "text-yellow-500 font-bold"
            else:
                inv_data["status_label"] = "EM ABERTO (FUTURA)"
                inv_data["class"] = "text-gray-400"

            info["invoices"].append(inv_data)
            info["total_pending"] += float(inv['amount'])

        if info["total_pending"] > 0:
            info["total_annual_discounted"] = info["total_pending"] * 0.90
            info["annual_savings"] = info["total_pending"] - info["total_annual_discounted"]
    except Exception as e: pass
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()
    return info

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
            return User(u['id'], u['name'], u['email'], role, u['status'])
        return None
    except Exception: return None
    finally:
        if db_pool and conn: db_pool.putconn(conn) 
        elif conn: conn.close()

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
        return redirect(url_for('admin_page'))
    return render_template('login.html')

@app.route('/briefing')
@login_required
def briefing_page():
    if current_user.status == 'pendente': return redirect(url_for('admin_page'))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM briefings WHERE client_id = %s", (current_user.id,))
        if cur.fetchone(): return redirect(url_for('admin_page'))
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()
    return render_template('briefing.html', user=current_user)

@app.route('/admin')
@login_required
def admin_page():
    conn = get_db_connection()
    fin_dashboard = get_financial_dashboard(current_user.id)
    
    stats = {
        "users": 0, "orders": 0, "revenue": 0.0, 
        "status_projeto": "AGUARDANDO", "revisoes": 3,
        "briefing_data": None, "financeiro": fin_dashboard,
        "pending_setup": False, "setup_order_id": None, "setup_value": 0.0,
        "available_addons": [], "briefing_status": "pending"
    }
    
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
            
            cur.execute("SELECT id, total_setup FROM orders WHERE client_id = %s AND payment_status = 'pending' ORDER BY id ASC LIMIT 1", (current_user.id,))
            pending_order = cur.fetchone()
            if pending_order:
                stats['pending_setup'] = True
                stats['setup_order_id'] = pending_order['id']
                stats['setup_value'] = float(pending_order['total_setup'])
            
            try:
                cur.execute("SELECT id, name, price_setup, price_monthly, description FROM addons")
                stats['available_addons'] = [dict(a) for a in cur.fetchall()]
            except: stats['available_addons'] = []

            # ATUALIZADO: BUSCA url_versao E TRATA O SKIP
            cur.execute("""
                SELECT status, revisoes_restantes, colors, style_preference, site_sections, url_versao 
                FROM briefings WHERE client_id = %s
            """, (current_user.id,))
            briefing = cur.fetchone()
            
            if briefing:
                stats["briefing_status"] = briefing['status']
                stats["status_projeto"] = briefing['status'].upper().replace('_', ' ')
                stats["revisoes"] = briefing['revisoes_restantes']
                stats["briefing_data"] = {
                    "colors": briefing['colors'],
                    "style": briefing['style_preference'],
                    "sections": briefing['site_sections'],
                    "url_versao": briefing.get('url_versao') # URL da Versão
                }
            
            if db_pool: db_pool.putconn(conn)
            elif conn: conn.close()
    except Exception as e:
        if db_pool and conn: db_pool.putconn(conn)
        print(f"Erro Admin: {e}")
    return render_template('admin.html', user=current_user, stats=stats)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

# --- ROTAS DE API ---

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
                user_obj = User(user_data['id'], user_data['name'], user_data['email'], 'user', user_data['status'])
                login_user(user_obj)
                if user_data['status'] == 'pendente': return jsonify({"message": "Redirecionando", "redirect": "/admin"})
                cur.execute("SELECT id FROM briefings WHERE client_id = %s", (user_data['id'],))
                has_briefing = cur.fetchone()
                redirect_url = "/admin" if has_briefing else "/briefing"
                return jsonify({"message": "Sucesso", "redirect": redirect_url})
        return jsonify({"error": "Credenciais inválidas"}), 401
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/briefing/save', methods=['POST'])
@login_required
def save_briefing():
    try:
        # Pega os campos antigos
        colors = request.form.get('colors')
        style = request.form.get('style')
        sections = request.form.get('sections')
        
        # Pega os NOVOS campos e concatena para não perder info se não tiver coluna
        refs = request.form.get('references', '')
        diffs = request.form.get('diferenciais', '')
        insta = request.form.get('social_insta', '')
        wa = request.form.get('contact_wa', '')

        # Concatena Style e Sections com as novas infos para garantir que sejam salvas
        if refs: style += f" | REFERÊNCIAS: {refs}"
        if diffs: sections += f" | DIFERENCIAIS: {diffs}"
        if insta or wa: sections += f" | CONTATOS: Insta {insta} / WhatsApp {wa}"

        file_names = []
        if 'files' in request.files:
            files = request.files.getlist('files')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(f"{current_user.id}_{file.filename}")
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    file_names.append(filename)

        conn = get_db_connection()
        cur = conn.cursor()
        
        # Verifica se já existe para fazer update (caso estivesse skipped)
        cur.execute("SELECT id FROM briefings WHERE client_id = %s", (current_user.id,))
        exists = cur.fetchone()

        if exists:
            cur.execute("""
                UPDATE briefings 
                SET colors=%s, style_preference=%s, site_sections=%s, uploaded_files=%s, status='ativo'
                WHERE client_id=%s
            """, (colors, style, sections, ",".join(file_names), current_user.id))
        else:
            cur.execute("""
                INSERT INTO briefings (client_id, colors, style_preference, site_sections, uploaded_files, ai_generated_prompt, status, revisoes_restantes)
                VALUES (%s, %s, %s, %s, %s, '', 'ativo', 3)
            """, (current_user.id, colors, style, sections, ",".join(file_names)))
            
        conn.commit()
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

        return jsonify({"success": True, "redirect": "/admin"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- NOVA ROTA: PULAR BRIEFING ---
@app.route('/api/briefing/skip', methods=['POST'])
@login_required
def skip_briefing():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Cria um briefing vazio com status 'skipped'
        cur.execute("""
            INSERT INTO briefings (client_id, colors, style_preference, site_sections, status, revisoes_restantes)
            VALUES (%s, 'A definir', 'A definir', 'A definir', 'skipped', 3)
            ON CONFLICT (id) DO NOTHING -- Ignora se já existir
        """, (current_user.id,))
        
        conn.commit()
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/briefing/update', methods=['POST'])
@login_required
def update_briefing():
    fin_status = get_financial_dashboard(current_user.id)
    if fin_status['status_global'] == 'overdue':
        return jsonify({"error": "Acesso bloqueado por pendência financeira."}), 403

    data = request.json
    colors = data.get('colors')
    style = data.get('style')
    sections = data.get('sections')

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT revisoes_restantes, status FROM briefings WHERE client_id = %s", (current_user.id,))
        res = cur.fetchone()
        
        if not res: return jsonify({"error": "Briefing não encontrado"}), 404
        
        revisoes = res['revisoes_restantes']
        status = res['status']
        
        # Se estava skipped, agora vira ativo
        new_status = 'ativo' if status == 'skipped' else status
        
        if revisoes <= 0: return jsonify({"error": "Limite de alterações atingido."}), 403

        cur.execute("""
            UPDATE briefings 
            SET colors = %s, style_preference = %s, site_sections = %s, revisoes_restantes = revisoes_restantes - 1, status = %s
            WHERE client_id = %s
        """, (colors, style, sections, new_status, current_user.id))
        
        conn.commit()
        
        status_changed = (status == 'skipped')
        
        return jsonify({"success": True, "revisoes_restantes": revisoes - 1, "status_changed": status_changed})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

# --- ROTA EMERGÊNCIA DB (ATUALIZADA) ---
@app.route('/fix-db')
def fix_db():
    conn = get_db_connection()
    if not conn: return "Erro ao conectar no banco"
    try:
        cur = conn.cursor()
        
        # 1. Tabela Invoices
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                client_id INTEGER REFERENCES clients(id),
                amount DECIMAL(10,2) NOT NULL,
                due_date DATE NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                paid_at TIMESTAMP
            );
        """)
        
        # 2. Tabela Addons
        cur.execute("""
            CREATE TABLE IF NOT EXISTS addons (
                id SERIAL PRIMARY KEY,
                product_id INTEGER,
                name VARCHAR(100) NOT NULL,\r\n                description TEXT,
                price_setup DECIMAL(10,2) NOT NULL,
                price_monthly DECIMAL(10,2) DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                prazo_addons INTEGER DEFAULT 2
            );
        """)

        # 3. Garante colunas legacy
        cur.execute("ALTER TABLE briefings ADD COLUMN IF NOT EXISTS revisoes_restantes INTEGER DEFAULT 3;")
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS document VARCHAR(50);")

        conn.commit()
        return "✅ Banco Atualizado com Sucesso! Tabelas 'invoices' e 'addons' verificadas."
    except Exception as e:
        conn.rollback()
        return f"❌ Erro ao atualizar DB: {e}"
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/request_reset', methods=['POST'])
def request_reset():
    email = request.json.get('email')
    if not email: return jsonify({"message": "Informe o e-mail"}), 400
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id FROM clients WHERE email = %s", (email,))
        if cur.fetchone():
            s = URLSafeTimedSerializer(app.secret_key)
            token = s.dumps(email, salt='recover-key')
            enviar_email(email, f"{BASE_URL}/login?reset_token={token}")
        return jsonify({"status": "success", "message": "Se o e-mail existir, um link foi enviado."})
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/reset_password_confirm', methods=['POST'])
def reset_password_confirm():
    token = request.json.get('token')
    pwd = request.json.get('password')
    s = URLSafeTimedSerializer(app.secret_key)
    try:
        email = s.loads(token, salt='recover-key', max_age=3600)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE clients SET password_hash = %s WHERE email = %s", (generate_password_hash(pwd), email))
        conn.commit()
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()
        return jsonify({"status": "success"})
    except: return jsonify({"message": "Erro"}), 400

@app.route('/api/pay_setup', methods=['POST'])
@login_required
def pay_setup():
    data = request.json
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT total_setup FROM orders WHERE id = %s", (data.get('order_id'),))
        order = cur.fetchone()
        if not order: return jsonify({"error": "Erro"}), 404
        pref = mp_sdk.preference().create({
            "items": [{"title": "Setup", "quantity": 1, "unit_price": float(order['total_setup'])}],
            "payer": {"email": current_user.email},
            "external_reference": str(data.get('order_id'))
        })
        return jsonify({"checkout_url": pref["response"]["init_point"]})
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/buy_addon', methods=['POST'])
@login_required
def buy_addon():
    addon_id = request.json.get('addon_id')
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT name, price_setup, price_monthly FROM addons WHERE id = %s", (addon_id,))
        addon = cur.fetchone()
        
        if not addon: return jsonify({"error": "Item inválido"}), 400

        cur.execute("""
            INSERT INTO orders (client_id, product_id, selected_addons, total_setup, total_monthly, payment_status, created_at)
            VALUES (%s, NULL, %s, %s, %s, 'pending', NOW())
            RETURNING id
        """, (current_user.id, json.dumps([addon_id]), addon['price_setup'], addon['price_monthly']))
        new_order_id = cur.fetchone()['id']
        conn.commit()

        unit_price = float(addon['price_setup'])

        preference_data = {
            "items": [{"id": f"ADDON-{new_order_id}", "title": f"Upgrade: {addon['name']}", "quantity": 1, "currency_id": "BRL", "unit_price": unit_price}],
            "payer": {"name": current_user.name, "email": current_user.email},
            "external_reference": str(new_order_id),
            "payment_methods": {"excluded_payment_types": [{"id": "credit_card"}], "installments": 1}
        }
        pref = mp_sdk.preference().create(preference_data)
        return jsonify({"checkout_url": pref["response"]["init_point"]})
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/pay_monthly', methods=['POST'])
@login_required
def pay_monthly():
    if not mp_sdk: return jsonify({"error": "Mercado Pago Offline"}), 500
    
    data = request.json
    invoice_id = data.get('invoice_id')
    
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, amount, due_date FROM invoices WHERE id = %s AND client_id = %s AND status = 'pending'", (invoice_id, current_user.id))
        invoice = cur.fetchone()
        
        if not invoice:
            return jsonify({"error": "Fatura não encontrada ou já paga."}), 404

        unit_price = float(invoice['amount'])

        preference_data = {
            "items": [{"id": f"INV-{invoice['id']}", "title": f"Mensalidade Leanttro - Venc: {invoice['due_date']}", "quantity": 1, "currency_id": "BRL", "unit_price": unit_price}],
            "payer": {
                "name": current_user.name,
                "email": current_user.email
            },
            "payment_methods": {
                "excluded_payment_types": [{"id": "credit_card"}],
                "installments": 1
            },
            "external_reference": f"INV-{invoice['id']}" 
        }

        pref = mp_sdk.preference().create(preference_data)
        
        return jsonify({
            "checkout_url": pref["response"]["init_point"],
            "invoice_id": invoice_id
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/pay_annual', methods=['POST'])
@login_required
def pay_annual():
    if not mp_sdk: return jsonify({"error": "Mercado Pago Offline"}), 500
    
    fin = get_financial_dashboard(current_user.id)
    total_discounted = fin['total_annual_discounted']
    
    if total_discounted <= 0:
        return jsonify({"error": "Não há débitos pendentes."}), 400

    unit_price = float(f"{total_discounted:.2f}")

    preference_data = {
        "items": [{"id": "ANNUAL", 
            "title": f"Antecipação Anual Leanttro - {len(fin['invoices'])} Parcelas", 
            "quantity": 1, 
            "currency_id": "BRL", 
            "unit_price": unit_price
        }],
        "payer": {
            "name": current_user.name,
            "email": current_user.email
        },
        "payment_methods": {
            "excluded_payment_types": [{"id": "credit_card"}],
            "installments": 1
        },
        "external_reference": f"ANNUAL-{current_user.id}"
    }
    
    try:
        pref = mp_sdk.preference().create(preference_data)
        return jsonify({"checkout_url": pref["response"]["init_point"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/chat', methods=['POST'])
def handle_chat():
    if not chat_model: return jsonify({'reply': 'IA Offline'})
    try:
        data = request.json
        chat = chat_model.start_chat(history=[])
        response = chat.send_message(data.get('message', 'Olá'))
        return jsonify({'reply': response.text})
    except: return jsonify({'reply': 'Erro'})

@app.route('/api/briefing/chat', methods=['POST'])
@login_required
def briefing_chat():
    try:
        model = genai.GenerativeModel(SELECTED_MODEL_NAME)
        response = model.generate_content(f"Ajude com briefing: {request.json.get('message')}")
        return jsonify({"reply": response.text})
    except: return jsonify({"reply": "Erro"})

@app.route('/api/contract/download', methods=['GET'])
@login_required
def download_contract_real():
    # Retorna PDF genérico para manter funcionalidade
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer)
    p.drawString(100, 750, "Contrato Leanttro")
    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="contrato.pdf", mimetype='application/pdf')

# Webhooks e outros métodos auxiliares mantidos...
@app.route('/api/webhook/mercadopago', methods=['POST'])
def mercadopago_webhook():
    topic = request.args.get('topic') or request.args.get('type')
    p_id = request.args.get('id') or request.args.get('data.id')

    if topic == 'payment' and p_id and mp_sdk:
        try:
            payment_info = mp_sdk.payment().get(p_id)
            if payment_info["status"] == 200:
                data = payment_info["response"]
                status = data['status']
                ref = data['external_reference']
                
                if status == 'approved' and ref:
                    conn = get_db_connection()
                    cur = conn.cursor()
                    
                    if ref.startswith('INV-'): # Mensalidade
                        invoice_id = ref.split('-')[1]
                        cur.execute("UPDATE invoices SET status = 'paid', paid_at = NOW() WHERE id = %s", (invoice_id,))
                    
                    elif ref.startswith('ADDON-'): # Compra de Addon dentro do painel
                        order_id = ref.split('-')[1] # No create addon usamos ADDON-OrderID
                        # Atualiza a order específica
                        cur.execute("UPDATE orders SET payment_status = 'approved' WHERE id = %s", (order_id,))
                        
                    elif ref.startswith('ANNUAL-'): # Pagamento Anual
                        client_id_webhook = ref.split('-')[1]
                        # Paga TODAS as faturas pendentes desse cliente
                        cur.execute("UPDATE invoices SET status = 'paid', paid_at = NOW() WHERE client_id = %s AND status = 'pending'", (client_id_webhook,))

                    else: # Setup Inicial (ID do pedido puro)
                        cur.execute("UPDATE orders SET payment_status = 'approved' WHERE id = %s", (ref,))
                        cur.execute("UPDATE clients SET status = 'active' WHERE id = (SELECT client_id FROM orders WHERE id = %s)", (ref,))
                    
                    conn.commit()
                    if db_pool and conn: db_pool.putconn(conn)
                    elif conn: conn.close()
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            print(f"Erro Webhook: {e}")
            return jsonify({"error": str(e)}), 500
    
    return jsonify({"status": "ignored"}), 200

# --- ROTA LEGADA (ATUALIZADA) ---
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
        p.drawString(50, y, "CLÁUSULAS GERAIS E ESCOPO")
        p.setFont("Helvetica", 10)
        y -= 20
        p.drawString(50, y, "1. O CONTRATANTE tem direito a 03 (três) rodadas completas de revisão.")
        y -= 15
        p.drawString(50, y, "2. A mensalidade cobre: Hospedagem, Certificado de Segurança (SSL) e Suporte Técnico.")
        y -= 15
        p.drawString(50, y, "3. O domínio (ex: .com.br) deve ser adquirido pelo cliente. A configuração técnica é gratuita.")
        y -= 15
        p.drawString(50, y, "4. Os prazos de entrega contam apenas após o envio de todo material pelo cliente.")
        
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
            return jsonify({"error": "E-mail já cadastrado. Faça login."}), 400
        else:
            hashed = generate_password_hash(client['password'])
            
            document = client.get('document', '') 
            
            cur.execute("""
                INSERT INTO clients (name, email, whatsapp, document, password_hash, status, created_at)
                VALUES (%s, %s, %s, %s, %s, 'pendente', NOW())
                RETURNING id
            """, (client['name'], client['email'], client['whatsapp'], document, hashed))
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
        
        webhook_url = "https://www.leanttro.com/api/webhook/mercadopago"

        # --- VALOR REAL (OFICIAL) ---
        unit_price = total_setup
        
        preference_data = {
            "items": [{"id": str(cart['product_id']), "title": f"PROJETO WEB #{order_id} (TESTE)", "quantity": 1, "unit_price": unit_price}],
            "payer": {"name": client['name'], "email": client['email']},
            "external_reference": str(order_id),
            "back_urls": {
                "success": "https://leanttro.com/login", 
                "failure": "https://leanttro.com/cadastro",
                "pending": "https://leanttro.com/cadastro"
            },
            "notification_url": webhook_url,
            "auto_return": "approved"
        }
        
        pref = mp_sdk.preference().create(preference_data)
        
        return jsonify({"checkout_url": pref["response"]["init_point"]})

    except Exception as e:
        conn.rollback()
        print(f"Erro Signup Checkout: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

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
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/cases', methods=['GET'])
def get_cases():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de Conexão"}), 500
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, url, foto_site FROM \"case\" ORDER BY id DESC")
        raw_cases = cur.fetchall()
        
        cases = []
        for c in raw_cases:
            case_dict = dict(c)
            if case_dict['foto_site'] and not case_dict['foto_site'].startswith('http'):
                case_dict['foto_site'] = f"{DIRECTUS_ASSETS_URL}{case_dict['foto_site']}"
            cases.append(case_dict)
        
        return jsonify(cases)
    except Exception as e:
        print(f"Erro ao buscar cases: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)