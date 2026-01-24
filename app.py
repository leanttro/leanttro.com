import os
import re
import io
import json
import psycopg2
import psycopg2.extras
from psycopg2 import pool
from datetime import datetime, timedelta, date
# ADICIONADO: 'Response' para servir XML e TXT corretamente
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, session, abort, send_from_directory, Response
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from groq import Groq
import mercadopago
from dotenv import load_dotenv
import traceback
import uuid 

# --- IMPORTAÇÕES PARA EMAIL ---
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from itsdangerous import URLSafeTimedSerializer

# Importações para PDF
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import locale

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
BASE_URL = os.getenv('APP_BASE_URL', 'https://leanttro.com')

# --- CONFIGURAÇÃO SMTP (EMAIL) ---
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))

# --- CONFIGURAÇÃO DB POOL ---
db_pool = None

# --- FUNÇÃO DE INICIALIZAÇÃO DO BANCO (AUTO-CORREÇÃO) ---
def init_db():
    """Garante que a tabela invoices exista, addons, colunas novas do briefing e clients_bot"""
    global db_pool
    try:
        if DB_URL:
            db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, DB_URL)
            print("✅ Pool de Conexões criado com sucesso")
            
            conn = db_pool.getconn()
            try:
                cur = conn.cursor()
                
                # 1. Tabela Invoices
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

                # 2. Tabela Addons
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS addons (
                        id SERIAL PRIMARY KEY,
                        product_id INTEGER,
                        name VARCHAR(100) NOT NULL,
                        description TEXT,
                        price_setup DECIMAL(10,2) NOT NULL,
                        price_monthly DECIMAL(10,2) DEFAULT 0,
                        is_active BOOLEAN DEFAULT TRUE,
                        prazo_addons INTEGER DEFAULT 2
                    );
                """)

                # 3. Tabela Briefings
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS briefings (
                        id SERIAL PRIMARY KEY,
                        client_id INTEGER,
                        colors TEXT,
                        style_preference TEXT,
                        site_sections TEXT,
                        uploaded_files TEXT,
                        ai_generated_prompt TEXT,
                        status VARCHAR(50) DEFAULT 'ativo',
                        revisoes_restantes INTEGER DEFAULT 3,
                        url_versao TEXT
                    );
                """)
                
                # 4. Tabela Clients Bot (NOVA - Para Máquina de Estados)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS clients_bot (
                        id SERIAL PRIMARY KEY,
                        session_uuid VARCHAR(255) UNIQUE NOT NULL,
                        name VARCHAR(255),
                        email VARCHAR(255),
                        whatsapp VARCHAR(50),
                        dor_principal TEXT,
                        accepts_marketing BOOLEAN,
                        current_step VARCHAR(50) DEFAULT 'START',
                        chat_history JSONB DEFAULT '[]',
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                """)

                # Fallback: Adições de colunas em tabelas existentes
                try:
                    cur.execute("ALTER TABLE briefings ADD COLUMN IF NOT EXISTS revisoes_restantes INTEGER DEFAULT 3;")
                except Exception: pass
                
                try:
                    cur.execute("ALTER TABLE briefings ADD COLUMN IF NOT EXISTS url_versao TEXT;")
                except Exception: pass
                
                crm_cols = [
                    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS temperatura VARCHAR(50);",
                    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS dor_principal TEXT;",
                    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS empresa_ramo VARCHAR(100);",
                    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS cargo VARCHAR(100);",
                    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS whatsapp VARCHAR(50);"
                ]
                for sql in crm_cols:
                    try:
                        cur.execute(sql)
                    except: pass 

                conn.commit()
                print("✅ [SISTEMA] Tabelas verificadas/criadas com sucesso.")
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

# --- CONFIGURAÇÃO GROQ ---
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
groq_client = None

if GROQ_API_KEY:
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
        print("🎯 GROQ CLIENTE INICIALIZADO (Llama 3.3 70b)")
    except Exception as e:
        print(f"❌ Erro ao iniciar Groq: {e}")
else:
    print("❌ Nenhuma API Key da Groq encontrada.")


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
        return True
    except Exception as e:
        print(f"❌ Erro ao enviar e-mail: {e}")
        return False

# --- FUNÇÃO: GARANTIR FATURAS FUTURAS ---
def ensure_future_invoices(client_id):
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
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
            if o.get('order_date'):
                first_order_date = o['order_date'].date()
            if val_order > 0:
                monthly_price = val_order
                break
            elif val_product > 0:
                monthly_price = val_product
                break
        
        if monthly_price <= 0:
            cur.execute("SELECT price_monthly FROM products WHERE is_active = TRUE AND price_monthly > 0 LIMIT 1")
            default_prod = cur.fetchone()
            if default_prod:
                monthly_price = float(default_prod['price_monthly'])

        if monthly_price <= 0:
            return

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
                    if target_due_date.month == 12:
                        target_due_date = date(target_due_date.year + 1, 1, 10)
                    else:
                        target_due_date = date(target_due_date.year, target_due_date.month + 1, 10)
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
                    cur.execute("""
                        INSERT INTO invoices (client_id, amount, due_date, status)
                        VALUES (%s, %s, %s, 'pending')
                    """, (client_id, monthly_price, due_dt))
            
            conn.commit()

    except Exception as e:
        print(f"❌ ERRO CRÍTICO FATURAS: {e}")
        conn.rollback()
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

# --- FUNÇÃO: DASHBOARD FINANCEIRO COMPLETO ---
def get_financial_dashboard(client_id):
    ensure_future_invoices(client_id)
    conn = get_db_connection()
    info = {
        "status_global": "ok", 
        "message": "EM DIA",
        "invoices": [],
        "total_pending": 0.0,
        "total_annual_discounted": 0.0,
        "annual_savings": 0.0
    }
    
    if not conn: return info

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, amount, due_date, status 
            FROM invoices 
            WHERE client_id = %s AND status = 'pending' 
            ORDER BY due_date ASC
        """, (client_id,))
        raw_invoices = cur.fetchall()
        
        today = date.today()
        
        for inv in raw_invoices:
            due = inv['due_date']
            delta = (today - due).days
            
            inv_data = {
                "id": inv['id'],
                "amount": float(inv['amount']),
                "date_fmt": due.strftime('%d/%m/%Y'),
                "status_label": "A VENCER",
                "class": "text-white"
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

    except Exception as e:
        print(f"Erro Fin Dashboard: {e}")
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
    except Exception as e:
        return None
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
    if current_user.status == 'pendente':
        return redirect(url_for('admin_page'))

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM briefings WHERE client_id = %s", (current_user.id,))
        if cur.fetchone():
            return redirect(url_for('admin_page'))
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
        "briefing_data": None,
        "financeiro": fin_dashboard,
        "pending_setup": False,
        "setup_order_id": None,
        "setup_value": 0.0,
        "available_addons": [],
        "url_versao": None,
        "is_skipped": False
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
            
            cur.execute("""
                SELECT id, total_setup FROM orders 
                WHERE client_id = %s AND payment_status = 'pending' 
                ORDER BY id ASC LIMIT 1
            """, (current_user.id,))
            pending_order = cur.fetchone()
            
            if pending_order:
                stats['pending_setup'] = True
                stats['setup_order_id'] = pending_order['id']
                stats['setup_value'] = float(pending_order['total_setup'])
            
            try:
                cur.execute("SELECT id, name, price_setup, price_monthly, description FROM addons")
                stats['available_addons'] = [dict(a) for a in cur.fetchall()]
            except:
                stats['available_addons'] = []

            cur.execute("""
                SELECT status, revisoes_restantes, colors, style_preference, site_sections, url_versao
                FROM briefings WHERE client_id = %s
            """, (current_user.id,))
            briefing = cur.fetchone()
            
            if briefing:
                stats["status_projeto"] = briefing['status'].upper().replace('_', ' ')
                stats["revisoes"] = briefing['revisoes_restantes']
                stats["briefing_data"] = {
                    "colors": briefing['colors'],
                    "style": briefing['style_preference'],
                    "sections": briefing['site_sections']
                }
                stats["url_versao"] = briefing.get('url_versao')
                if briefing['status'] == 'skipped':
                    stats['is_skipped'] = True
            
            if db_pool: db_pool.putconn(conn)
            elif conn: conn.close()
    except Exception as e:
        if db_pool and conn: db_pool.putconn(conn)
        pass
    return render_template('admin.html', user=current_user, stats=stats)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

# --- ROTAS DE SEO ---
@app.route('/sitemap.xml')
def sitemap():
    """Gera sitemap XML dinâmico para indexação do Google"""
    base_url = BASE_URL
    today = date.today().isoformat()
    
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url>
            <loc>{base_url}/</loc>
            <lastmod>{today}</lastmod>
            <changefreq>daily</changefreq>
            <priority>1.0</priority>
        </url>
        <url>
            <loc>{base_url}/login</loc>
            <priority>0.5</priority>
        </url>
        <url>
            <loc>{base_url}/cadastro</loc>
            <priority>0.8</priority>
        </url>
        <url>
            <loc>{base_url}/blog</loc>
            <changefreq>daily</changefreq>
            <priority>0.9</priority>
        </url>
    </urlset>"""
    return Response(xml, mimetype='application/xml')

@app.route('/robots.txt')
def robots():
    """Diz ao Google o que pode ser lido"""
    base_url = BASE_URL
    text = f"""User-agent: *
Allow: /
Disallow: /admin
Disallow: /briefing
Sitemap: {base_url}/sitemap.xml
"""
    return Response(text, mimetype='text/plain')

# --- ROTAS DE BLOG (NOVO) ---
@app.route('/blog')
def blog_index():
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Pega posts publicados
        cur.execute("""
            SELECT title, slug, cover_image, description, published_at 
            FROM posts 
            WHERE status = 'published' 
            ORDER BY published_at DESC
        """)
        posts = cur.fetchall()
        # Tratamento da imagem (adiciona URL do Directus)
        for p in posts:
            if p['cover_image']:
                p['cover_image'] = f"{DIRECTUS_ASSETS_URL}{p['cover_image']}"
        
        return render_template('blog.html', posts=posts)
    except Exception as e:
        print(f"Erro Blog Index (Tabela não existe?): {e}")
        # Retorna array vazio para não quebrar se a tabela não existir
        return render_template('blog.html', posts=[])
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/blog/<slug>')
def blog_post(slug):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT title, content, cover_image, published_at, description, keywords 
            FROM posts 
            WHERE slug = %s AND status = 'published'
        """, (slug,))
        post = cur.fetchone()
        
        if not post: abort(404)

        if post['cover_image']:
            post['cover_image'] = f"{DIRECTUS_ASSETS_URL}{post['cover_image']}"
            
        return render_template('post.html', post=post)
    except Exception as e:
        print(f"Erro Blog Post: {e}")
        abort(404)
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

# --- ROTAS DE API (BACKEND) ---

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro DB"}), 500

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name, email, password_hash, status FROM clients WHERE email = %s", (email,))
        user_data = cur.fetchone()

        if user_data and user_data['password_hash']:
            if check_password_hash(user_data['password_hash'], password):
                user_obj = User(user_data['id'], user_data['name'], user_data['email'], 'user', user_data['status'])
                login_user(user_obj)
                
                if user_data['status'] == 'pendente':
                    return jsonify({"message": "Redirecionando para pagamento", "redirect": "/admin"})

                cur.execute("SELECT id FROM briefings WHERE client_id = %s", (user_data['id'],))
                has_briefing = cur.fetchone()
                
                redirect_url = "/admin" if has_briefing else "/briefing"
                return jsonify({"message": "Sucesso", "redirect": redirect_url})
        
        return jsonify({"error": "Credenciais inválidas"}), 401
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/request_reset', methods=['POST'])
def request_reset():
    email = request.json.get('email')
    if not email:
        return jsonify({"message": "Informe o e-mail"}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name FROM clients WHERE email = %s", (email,))
        user_data = cur.fetchone()
        
        if not user_data:
            return jsonify({"status": "success", "message": "Se o e-mail existir, um link foi enviado."})

        s = URLSafeTimedSerializer(app.secret_key)
        token = s.dumps(email, salt='recover-key')
        
        reset_link = f"{BASE_URL}/login?reset_token={token}"
        enviado = enviar_email(email, reset_link)
        
        if enviado:
            return jsonify({"status": "success", "message": "Link de recuperação enviado para seu e-mail."})
        else:
            return jsonify({"status": "error", "message": "Erro ao enviar e-mail. Contate o suporte."}), 500

    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/reset_password_confirm', methods=['POST'])
def reset_password_confirm():
    token = request.json.get('token')
    new_password = request.json.get('password')
    
    if not token or not new_password:
        return jsonify({"message": "Dados inválidos"}), 400

    s = URLSafeTimedSerializer(app.secret_key)
    try:
        email = s.loads(token, salt='recover-key', max_age=3600)
    except:
        return jsonify({"message": "Link inválido ou expirado."}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        hashed = generate_password_hash(new_password)
        cur.execute("UPDATE clients SET password_hash = %s WHERE email = %s", (hashed, email))
        conn.commit()
        
        if cur.rowcount > 0:
            return jsonify({"status": "success", "message": "Senha atualizada com sucesso!"})
        else:
            return jsonify({"message": "Usuário não encontrado."}), 404
            
    except Exception as e:
        return jsonify({"message": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/pay_setup', methods=['POST'])
@login_required
def pay_setup():
    data = request.json
    order_id = data.get('order_id')
    
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT total_setup, product_id FROM orders WHERE id = %s AND client_id = %s AND payment_status = 'pending'", (order_id, current_user.id))
        order = cur.fetchone()
        
        if not order:
            return jsonify({"error": "Pedido não encontrado ou já pago."}), 404

        unit_price = float(order['total_setup'])
        preference_data = {
            "items": [{"id": f"SETUP-{order_id}", "title": f"Ativação do Projeto #{order_id}", "quantity": 1, "currency_id": "BRL", "unit_price": unit_price}],
            "payer": {"name": current_user.name, "email": current_user.email},
            "external_reference": str(order_id),
            "payment_methods": {"excluded_payment_types": [{"id": "credit_card"}], "installments": 1}
        }
        
        pref = mp_sdk.preference().create(preference_data)
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
        
        if not addon:
            return jsonify({"error": "Item inválido"}), 400

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

@app.route('/api/briefing/update', methods=['POST'])
@login_required
def update_briefing():
    fin_status = get_financial_dashboard(current_user.id)
    if fin_status['status_global'] == 'overdue':
        return jsonify({"error": "Acesso bloqueado por pendência financeira. Regularize para editar."}), 403

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
        status_atual = res['status']

        if revisoes <= 0:
            return jsonify({"error": "Limite de alterações atingido."}), 403

        novo_status = 'ativo' if status_atual == 'skipped' else status_atual

        cur.execute("""
            UPDATE briefings 
            SET colors = %s, style_preference = %s, site_sections = %s, revisoes_restantes = revisoes_restantes - 1, status = %s
            WHERE client_id = %s
        """, (colors, style, sections, novo_status, current_user.id))
        
        conn.commit()
        return jsonify({"success": True, "revisoes_restantes": revisoes - 1})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/briefing/skip', methods=['POST'])
@login_required
def skip_briefing():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO briefings (client_id, colors, style_preference, site_sections, uploaded_files, ai_generated_prompt, status, revisoes_restantes)
            VALUES (%s, 'Pendente', 'Pendente (Pulado)', 'Pendente (Pulado)', '', 'User skipped briefing', 'skipped', 3)
        """, (current_user.id,))
        conn.commit()
        return jsonify({"success": True, "redirect": "/admin"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
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
        
        if not invoice: return jsonify({"error": "Fatura não encontrada ou já paga."}), 404

        unit_price = float(invoice['amount'])
        preference_data = {
            "items": [{"id": f"INV-{invoice['id']}", "title": f"Mensalidade Leanttro - Venc: {invoice['due_date']}", "quantity": 1, "currency_id": "BRL", "unit_price": unit_price}],
            "payer": {"name": current_user.name, "email": current_user.email},
            "payment_methods": {"excluded_payment_types": [{"id": "credit_card"}], "installments": 1},
            "external_reference": f"INV-{invoice['id']}" 
        }

        pref = mp_sdk.preference().create(preference_data)
        return jsonify({"checkout_url": pref["response"]["init_point"], "invoice_id": invoice_id})
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
    if total_discounted <= 0: return jsonify({"error": "Não há débitos pendentes."}), 400

    unit_price = float(f"{total_discounted:.2f}")
    preference_data = {
        "items": [{"id": "ANNUAL", 
            "title": f"Antecipação Anual Leanttro - {len(fin['invoices'])} Parcelas", 
            "quantity": 1, 
            "currency_id": "BRL", 
            "unit_price": unit_price
        }],
        "payer": {"name": current_user.name, "email": current_user.email},
        "payment_methods": {"excluded_payment_types": [{"id": "credit_card"}], "installments": 1},
        "external_reference": f"ANNUAL-{current_user.id}" 
    }
    
    try:
        pref = mp_sdk.preference().create(preference_data)
        return jsonify({"checkout_url": pref["response"]["init_point"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/contract/download', methods=['GET'])
@login_required
def download_contract_real():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão"}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.name, c.document, c.email,
                   p.name as product_name, p.slug as product_slug,
                   o.total_setup, o.total_monthly, o.selected_addons, o.created_at
            FROM clients c
            JOIN orders o ON c.id = o.client_id
            JOIN products p ON o.product_id = p.id
            WHERE c.id = %s
            ORDER BY o.created_at DESC LIMIT 1
        """, (current_user.id,))
        data = cur.fetchone()
        
        if not data: return jsonify({"error": "Nenhum contrato ativo encontrado."}), 404

        try:
            addons_list = json.loads(data['selected_addons']) if data['selected_addons'] else []
            qtd_addons = len(addons_list)
        except: qtd_addons = 0
            
        slug = data.get('product_slug', '').lower()
        if 'loja' in slug or 'virtual' in slug or 'ecommerce' in slug: prazo_base = 20
        elif 'custom' in slug or 'corp' in slug: prazo_base = 30
        else: prazo_base = 15
            
        dias_adicionais = qtd_addons * 2
        prazo_final = prazo_base + dias_adicionais

        buffer = io.BytesIO()
        p = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        margin_left = 50
        y = height - 50
        line_height = 14

        p.setFont("Helvetica-Bold", 14)
        p.drawCentredString(width / 2, y, "CONTRATO DE PRESTAÇÃO DE SERVIÇOS")
        y -= 20
        p.drawCentredString(width / 2, y, "DE DESENVOLVIMENTO DE SOFTWARE E LICENÇA DE USO")
        y -= 40
        p.setFont("Helvetica", 10)
        
        texto_contratada = [
            "Pelo presente instrumento particular, de um lado:",
            "CONTRATADA: LEANTTRO DIGITAL SOLUTIONS, nome fantasia de LEANDRO ANDRADE DE OLIVEIRA,",
            f"pessoa jurídica de direito privado, inscrita no CNPJ sob o nº {COMPANY_CNPJ},",
            "doravante denominada simplesmente LEANTTRO."
        ]
        
        for linha in texto_contratada:
            p.drawString(margin_left, y, linha)
            y -= line_height
        y -= 10
        
        doc_cliente = data.get('document') or "Não informado"
        texto_contratante = [
            "De outro lado:",
            f"CONTRATANTE: {data['name'].upper()},",
            f"Inscrito(a) no CPF/CNPJ sob o nº {doc_cliente},",
            f"E-mail de contato: {data.get('email')}.",
            "Doravante denominado(a) simplesmente CONTRATANTE."
        ]

        for linha in texto_contratante:
            p.drawString(margin_left, y, linha)
            y -= line_height

        y -= 20
        p.drawString(margin_left, y, "Resolvem as Partes, de comum acordo, celebrar o presente Contrato, regido pelas seguintes cláusulas:")
        y -= 30

        def draw_clause_title(title, current_y):
            p.setFont("Helvetica-Bold", 11)
            p.drawString(margin_left, current_y, title)
            return current_y - 15

        def draw_clause_text(text_lines, current_y):
            p.setFont("Helvetica", 10)
            for line in text_lines:
                p.drawString(margin_left, current_y, line)
                current_y -= 12
            return current_y - 10

        y = draw_clause_title("CLÁUSULA PRIMEIRA - DO OBJETO", y)
        y = draw_clause_text([
            f"1.1. O presente contrato tem por objeto o desenvolvimento e licenciamento do projeto: {data['product_name'].upper()}.",
            "1.2. O serviço inclui a configuração de servidor, instalação de certificado SSL e estruturação visual",
            "conforme briefing preenchido pelo CONTRATANTE."
        ], y)

        y = draw_clause_title("CLÁUSULA SEGUNDA - DOS PRAZOS DE ENTREGA", y)
        y = draw_clause_text([
            f"2.1. O prazo estimado para entrega da primeira versão do projeto é de {prazo_final} dias úteis.",
            f"     (Base: {prazo_base} dias + {dias_adicionais} dias referentes a {qtd_addons} funcionalidades adicionais contratadas).",
            "2.2. A contagem do prazo inicia-se apenas após o envio completo de todo o material (textos e imagens)",
            "     necessário pelo CONTRATANTE através da plataforma ou e-mail."
        ], y)

        y = draw_clause_title("CLÁUSULA TERCEIRA - DO PREÇO E MENSALIDADE", y)
        y = draw_clause_text([
            f"3.1. Valor de Setup (Criação/Implementação): R$ {data['total_setup']:,.2f}",
            f"3.2. Valor da Mensalidade (Manutenção/Hospedagem): R$ {data['total_monthly']:,.2f}",
            "3.3. A mensalidade cobre: Hospedagem de alta performance, Certificado de Segurança (SSL),",
            "     Backup diário e Suporte Técnico via Helpdesk."
        ], y)

        y = draw_clause_title("CLÁUSULA QUARTA - DISPOSIÇÕES GERAIS E NOTA FISCAL", y)
        y = draw_clause_text([
            "4.1. O CONTRATANTE tem direito a 03 (três) rodadas completas de revisão do layout.",
            "4.2. Domínio: O endereço web (ex: .com.br) não está incluso e deve ser adquirido pelo CONTRATANTE.",
            "     A LEANTTRO realizará a configuração técnica do apontamento DNS gratuitamente.",
            f"4.3. NOTA FISCAL: A Nota Fiscal de Serviço (NFS-e) será emitida pela contratada ({COMPANY_CNPJ})",
            "     automaticamente após a entrega final e aceite do projeto ou pagamento integral do setup.",
            "4.4. A inadimplência superior a 10 dias acarretará na suspensão temporária dos serviços."
        ], y)

        y = draw_clause_title("CLÁUSULA QUINTA - DO FORO", y)
        y = draw_clause_text([
            "5.1. Fica eleito o foro da Comarca de São Paulo/SP para dirimir quaisquer dúvidas oriundas deste contrato."
        ], y)

        y -= 40
        p.setLineWidth(0.5)
        p.line(margin_left, y, margin_left + 200, y)
        p.setFont("Helvetica", 8)
        p.drawString(margin_left, y - 10, "LEANTTRO DIGITAL SOLUTIONS")
        p.drawString(margin_left, y - 20, "Leandro Andrade de Oliveira")
        p.line(width - margin_left - 200, y, width - margin_left, y)
        p.drawRightString(width - margin_left, y - 10, data['name'].upper())
        p.drawRightString(width - margin_left, y - 20, f"Doc: {doc_cliente}")
        
        try: locale.setlocale(locale.LC_TIME, 'pt_BR.utf8')
        except: pass
            
        data_atual = datetime.now().strftime("%d de %B de %Y")
        p.drawCentredString(width / 2, y - 60, f"São Paulo, {data_atual}")

        p.showPage()
        p.save()
        buffer.seek(0)
        filename = f"Contrato_Leanttro_{current_user.id}.pdf"
        return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Erro ao gerar contrato"}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

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
        return jsonify({"error": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

# --- WEBHOOK (ATIVAR CLIENTE) ---
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
                    if ref.startswith('INV-'):
                        invoice_id = ref.split('-')[1]
                        cur.execute("UPDATE invoices SET status = 'paid', paid_at = NOW() WHERE id = %s", (invoice_id,))
                    elif ref.startswith('ADDON-'):
                        order_id = ref.split('-')[1]
                        cur.execute("UPDATE orders SET payment_status = 'approved' WHERE id = %s", (order_id,))
                    elif ref.startswith('ANNUAL-'):
                        client_id_webhook = ref.split('-')[1]
                        cur.execute("UPDATE invoices SET status = 'paid', paid_at = NOW() WHERE client_id = %s AND status = 'pending'", (client_id_webhook,))
                    else:
                        cur.execute("UPDATE orders SET payment_status = 'approved' WHERE id = %s", (ref,))
                        cur.execute("UPDATE clients SET status = 'active' WHERE id = (SELECT client_id FROM orders WHERE id = %s)", (ref,))
                    
                    conn.commit()
                    if db_pool and conn: db_pool.putconn(conn)
                    elif conn: conn.close()
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"status": "ignored"}), 200

# --- SAVE BRIEFING (AGORA COM GROQ) ---
@app.route('/api/briefing/save', methods=['POST'])
@login_required
def save_briefing():
    try:
        colors = request.form.get('colors')
        style = request.form.get('style')
        sections = request.form.get('sections')
        benchmark = request.form.get('benchmark', '')
        diferenciais = request.form.get('diferenciais', '')
        instagram = request.form.get('instagram', '')
        whatsapp_contact = request.form.get('whatsapp_contact', '')
        
        file_names = []
        if 'files' in request.files:
            files = request.files.getlist('files')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(f"{current_user.id}_{file.filename}")
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    file_names.append(filename)

        final_style = f"{style}\n\n[INFO CONTATO & REDES]\nInstagram: {instagram}\nWhatsApp: {whatsapp_contact}"
        final_sections = f"{sections}\n\n[INFO ESTRATÉGICA]\nReferências (Benchmark): {benchmark}\nDiferenciais: {diferenciais}"

        tech_prompt_input = f"""
        ATUE COMO ARQUITETO DE SOFTWARE. Crie um prompt técnico:
        - CLIENTE: {current_user.name}
        - CORES: {colors}
        - ESTILO: {final_style}
        - SEÇÕES: {final_sections}
        - STACK: HTML, TailwindCSS, JS.
        - OUTPUT: Apenas o prompt técnico.
        """
        tech_prompt = "Erro ao gerar com IA."
        try:
            if groq_client:
                completion = groq_client.chat.completions.create(
                    messages=[{"role": "user", "content": tech_prompt_input}],
                    model="llama-3.3-70b-versatile"
                )
                tech_prompt = completion.choices[0].message.content
        except Exception as e:
            print(f"Erro IA Briefing: {e}")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO briefings (client_id, colors, style_preference, site_sections, uploaded_files, ai_generated_prompt, status, revisoes_restantes)
            VALUES (%s, %s, %s, %s, %s, %s, 'ativo', 3)
        """, (current_user.id, colors, final_style, final_sections, ",".join(file_names), tech_prompt))
        conn.commit()
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

        return jsonify({"success": True, "redirect": "/admin"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- CHATBOT: LÓGICA RÍGIDA (MÁQUINA DE ESTADOS) - CORRIGIDO ---
@app.route('/api/chat', methods=['POST'])
def handle_chat():
    if not groq_client:
        return jsonify({'reply': 'Serviço de IA Offline.'}), 503

    data_in = request.json
    user_message = data_in.get('message', '').strip()
    
    # 1. Gestão de Sessão
    if 'user_uuid' not in session:
        session['user_uuid'] = str(uuid.uuid4())
    session_uuid = session['user_uuid']

    conn = get_db_connection()
    if not conn: return jsonify({'reply': 'Erro de conexão.'}), 500

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # 2. Recuperar ou Criar Estado do Usuário
        cur.execute("SELECT * FROM clients_bot WHERE session_uuid = %s", (session_uuid,))
        user_state = cur.fetchone()

        if not user_state:
            cur.execute("""
                INSERT INTO clients_bot (session_uuid, current_step)
                VALUES (%s, 'START') RETURNING *
            """, (session_uuid,))
            conn.commit()
            user_state = cur.fetchone()

        current_step = user_state['current_step']
        reply_text = ""
        next_step = current_step

        # --- MÁQUINA DE ESTADOS ---

        # ESTADO: START -> Vai direto para pergunta de marketing
        if current_step == 'START':
            next_step = 'ASK_MARKETING'
            reply_text = "Olá! Sou o Lelis, da Leanttro. Antes de falarmos, posso te enviar novidades sobre tecnologia por aqui?"
            
            cur.execute("UPDATE clients_bot SET current_step = %s WHERE session_uuid = %s", (next_step, session_uuid))
            conn.commit()
            return jsonify({'reply': reply_text})

        # ESTADO: ASK_MARKETING
        elif current_step == 'ASK_MARKETING':
            # PROMPT CORRIGIDO PARA PORTUGUÊS
            try:
                analysis = groq_client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": "Analyze user input (Portuguese). 'Sim', 'Claro', 'Pode', 'Yes' -> consent=true, valid=true. 'Não', 'Jamais', 'No' -> consent=false, valid=true. Unclear/Garbage -> valid=false. Return JSON: {\"consent\": boolean, \"valid\": boolean}."},
                        {"role": "user", "content": user_message}
                    ],
                    model="llama-3.3-70b-versatile",
                    response_format={"type": "json_object"}
                )
                result = json.loads(analysis.choices[0].message.content)
                
                if result.get('valid'):
                    cur.execute("UPDATE clients_bot SET accepts_marketing = %s, current_step = 'ASK_NAME' WHERE session_uuid = %s", (result.get('consent'), session_uuid))
                    conn.commit()
                    reply_text = "Combinado! Agora, qual é o seu nome?"
                else:
                    reply_text = "Desculpe, não entendi. Posso enviar novidades? (Responda com 'Sim' ou 'Não')"
            except:
                 reply_text = "Não entendi. Digite apenas 'Sim' ou 'Não'."

        # ESTADO: ASK_NAME
        elif current_step == 'ASK_NAME':
            try:
                analysis = groq_client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": "Extract name from Portuguese text. Return JSON: {\"name\": string}. If no name found, return null name."},
                        {"role": "user", "content": user_message}
                    ],
                    model="llama-3.3-70b-versatile",
                    response_format={"type": "json_object"}
                )
                result = json.loads(analysis.choices[0].message.content)
                name = result.get('name')
                
                if name:
                    cur.execute("UPDATE clients_bot SET name = %s, current_step = 'ASK_EMAIL' WHERE session_uuid = %s", (name, session_uuid))
                    conn.commit()
                    reply_text = f"Prazer, {name}! Qual é o seu melhor e-mail?"
                else:
                    reply_text = "Poderia me dizer apenas seu nome para eu registrar aqui?"
            except:
                reply_text = "Qual é o seu nome?"

        # ESTADO: ASK_EMAIL
        elif current_step == 'ASK_EMAIL':
            try:
                analysis = groq_client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": "Extract email. Return JSON: {\"email\": string}. If invalid/missing, return null."},
                        {"role": "user", "content": user_message}
                    ],
                    model="llama-3.3-70b-versatile",
                    response_format={"type": "json_object"}
                )
                result = json.loads(analysis.choices[0].message.content)
                email = result.get('email')

                if email:
                    cur.execute("UPDATE clients_bot SET email = %s, current_step = 'ASK_WHATSAPP' WHERE session_uuid = %s", (email, session_uuid))
                    conn.commit()
                    reply_text = "Show! E qual seu WhatsApp (com DDD)?"
                else:
                    reply_text = "Parece que esse e-mail não é válido. Tenta de novo?"
            except:
                reply_text = "Por favor, digite um e-mail válido."

        # ESTADO: ASK_WHATSAPP
        elif current_step == 'ASK_WHATSAPP':
            try:
                analysis = groq_client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": "Extract phone number/whatsapp. Return JSON: {\"whatsapp\": string}. If invalid, return null."},
                        {"role": "user", "content": user_message}
                    ],
                    model="llama-3.3-70b-versatile",
                    response_format={"type": "json_object"}
                )
                result = json.loads(analysis.choices[0].message.content)
                whatsapp = result.get('whatsapp')

                if whatsapp:
                    cur.execute("UPDATE clients_bot SET whatsapp = %s, current_step = 'ASK_PAIN' WHERE session_uuid = %s", (whatsapp, session_uuid))
                    conn.commit()
                    reply_text = "Perfeito, dados anotados. Me conta: o que você precisa desenvolver hoje? (Site, App, Sistema...)"
                else:
                    reply_text = "Preciso de um número válido (com DDD) para seguirmos."
            except:
                reply_text = "Digite seu WhatsApp com DDD, por favor."

        # ESTADO: ASK_PAIN (Última etapa de coleta)
        elif current_step == 'ASK_PAIN':
            cur.execute("UPDATE clients_bot SET dor_principal = %s, current_step = 'FINISHED' WHERE session_uuid = %s", (user_message, session_uuid))
            conn.commit()
            
            # MUDANÇA SOLICITADA: Encerramento direto sem consultoria técnica
            reply_text = "Perfeito! Já registrei tudo aqui. Em breve um de nossos atendentes entrará em contato com você para apresentarmos a melhor solução. Obrigado!"

        # ESTADO: FINISHED (Chat Encerrado)
        elif current_step == 'FINISHED':
            # Evita alucinação: Resposta estática confirmando o recebimento
            reply_text = "Seu cadastro já foi recebido! Nossa equipe entrará em contato em breve."

        return jsonify({'reply': reply_text})

    except Exception as e:
        print(f"❌ ERRO CHAT: {e}")
        return jsonify({'reply': "Tive um erro interno. Podemos recomeçar?"}), 200
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/briefing/chat', methods=['POST'])
@login_required
def briefing_chat():
    data = request.json
    last_msg = data.get('message')
    try:
        if groq_client:
            completion = groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": "Você é LIA, especialista em Briefing. Ajude o cliente a definir o site."},
                    {"role": "user", "content": f"Cliente: {last_msg}"}
                ],
                model="llama-3.3-70b-versatile"
            )
            return jsonify({"reply": completion.choices[0].message.content})
        else:
            return jsonify({"reply": "IA Offline"})
    except:
        return jsonify({"reply": "Erro de conexão com a IA."})

# --- ROTA EMERGÊNCIA DB ---
@app.route('/fix-db')
def fix_db():
    init_db()
    return "✅ Banco Atualizado com Sucesso!"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)