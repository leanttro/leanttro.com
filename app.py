import os
import re
import io
import json
import psycopg2
import psycopg2.extras
from psycopg2 import pool # ADICIONADO: Importação do Pool
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

# --- CONFIGURAÇÃO DB POOL (ADICIONADO) ---
db_pool = None
try:
    if DB_URL:
        db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, DB_URL)
        print("✅ Pool de Conexões criado com sucesso")
    else:
        print("❌ DATABASE_URL não encontrada.")
except Exception as e:
    print(f"❌ Erro ao criar Pool: {e}")

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

        # --- ALTERAÇÃO: PERSONA LELIS CONSULTOR ---
        SYSTEM_PROMPT_LELIS = """
        VOCÊ É: Lelis, Consultor Executivo da Leanttro Digital.
        SUA MISSÃO: Fechar contratos de alto valor passando autoridade e segurança técnica.
        TOM: Profissional, Seguro, Educado e Direto. Não use gírias.

        TABELA DE PREÇOS:
        1. Site Institucional: De R$ 1.200 por R$ 499 (Promoção).
        2. Loja Virtual: R$ 999.
        3. Sistemas Custom: A partir de R$ 1.500.

        REGRAS:
        1. Respostas curtas e objetivas.
        2. Se perguntarem preço, apresente o valor e pergunte: "Posso verificar a disponibilidade da nossa equipe técnica para iniciar ainda esta semana?"
        3. Se o cliente tiver dúvidas técnicas, simplifique a explicação focando no benefício (ex: "Isso garante que seu site não saia do ar").
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
    def __init__(self, id, name, email, role='user'):
        self.id = id
        self.name = name
        self.email = email
        self.role = role

# --- ALTERAÇÃO: GET DB CONNECTION COM POOL ---
def get_db_connection():
    try:
        if db_pool:
            return db_pool.getconn()
        else:
            return psycopg2.connect(DB_URL) # Fallback se o pool falhar
    except Exception as e:
        print(f"❌ Erro de Conexão DB: {e}")
        return None

# --- NOVA FUNÇÃO: VERIFICAR STATUS FINANCEIRO ---
def get_financial_status(client_id):
    """
    Retorna o status financeiro do cliente.
    Status possíveis: 'ok', 'pending' (vencendo hoje/recente), 'overdue' (atrasado > 3 dias)
    """
    conn = get_db_connection()
    status_info = {
        "status": "ok", 
        "message": "EM DIA", 
        "due_date": None, 
        "amount": 0.0,
        "invoice_id": None
    }
    
    if not conn: return status_info

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Busca faturas pendentes ordenadas por vencimento
        cur.execute("""
            SELECT id, amount, due_date, status 
            FROM invoices 
            WHERE client_id = %s AND status = 'pending' 
            ORDER BY due_date ASC LIMIT 1
        """, (client_id,))
        invoice = cur.fetchone()
        
        if invoice:
            today = date.today()
            due_date = invoice['due_date']
            
            status_info["amount"] = float(invoice['amount'])
            status_info["due_date"] = due_date.strftime('%d/%m/%Y')
            status_info["invoice_id"] = invoice['id']
            
            delta_days = (today - due_date).days
            
            if delta_days > 3:
                status_info["status"] = "overdue"
                status_info["message"] = f"ATRASADO ({delta_days} DIAS)"
            elif delta_days >= 0:
                status_info["status"] = "pending"
                status_info["message"] = "VENCE HOJE" if delta_days == 0 else f"VENCEU HÁ {delta_days} DIA(S)"
            else:
                # Fatura futura, mas já gerada (ainda OK para o sistema de bloqueio, mas mostramos aviso se quiser)
                status_info["status"] = "ok" 
                status_info["message"] = f"PRÓXIMA: {status_info['due_date']}"

    except Exception as e:
        print(f"Erro Financial Status: {e}")
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()
        
    return status_info


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
        if db_pool and conn: db_pool.putconn(conn) # Devolve pro pool
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
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM briefings WHERE client_id = %s", (current_user.id,))
            if cur.fetchone():
                return redirect(url_for('admin_page'))
            else:
                return redirect(url_for('briefing_page'))
        finally:
            if db_pool and conn: db_pool.putconn(conn)
            elif conn: conn.close()
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
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()
    return render_template('briefing.html', user=current_user)

@app.route('/admin')
@login_required
def admin_page():
    conn = get_db_connection()
    # Pega status financeiro
    fin_status = get_financial_status(current_user.id)
    
    stats = {
        "users": 0, "orders": 0, "revenue": 0.0, 
        "status_projeto": "AGUARDANDO", "revisoes": 3,
        "briefing_data": None,
        "financeiro": fin_status # Adicionado ao objeto stats
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
                SELECT status, revisoes_restantes, colors, style_preference, site_sections 
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
            
            if db_pool: db_pool.putconn(conn)
            elif conn: conn.close()
            
    except Exception as e:
        print(f"Erro Admin: {e}")
        if db_pool and conn: db_pool.putconn(conn) # Garantia extra
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
                
                if user_data.get('status') == 'pendente':
                    return jsonify({"error": "Pagamento não confirmado. Aguarde o processamento."}), 403

                user_obj = User(user_data['id'], user_data['name'], user_data['email'])
                login_user(user_obj)
                
                cur.execute("SELECT id FROM briefings WHERE client_id = %s", (user_data['id'],))
                has_briefing = cur.fetchone()
                
                redirect_url = "/admin" if has_briefing else "/briefing"

                return jsonify({"message": "Sucesso", "redirect": redirect_url})
        
        return jsonify({"error": "Credenciais inválidas"}), 401
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

@app.route('/api/briefing/update', methods=['POST'])
@login_required
def update_briefing():
    # --- BLOQUEIO FINANCEIRO ---
    fin_status = get_financial_status(current_user.id)
    if fin_status['status'] == 'overdue':
        return jsonify({"error": "Acesso bloqueado por pendência financeira. Regularize para editar."}), 403
    # ---------------------------

    data = request.json
    colors = data.get('colors')
    style = data.get('style')
    sections = data.get('sections')

    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cur.execute("SELECT revisoes_restantes FROM briefings WHERE client_id = %s", (current_user.id,))
        res = cur.fetchone()
        
        if not res: return jsonify({"error": "Briefing não encontrado"}), 404
        
        revisoes = res['revisoes_restantes']
        if revisoes <= 0:
            return jsonify({"error": "Limite de alterações atingido."}), 403

        cur.execute("""
            UPDATE briefings 
            SET colors = %s, style_preference = %s, site_sections = %s, revisoes_restantes = revisoes_restantes - 1
            WHERE client_id = %s
        """, (colors, style, sections, current_user.id))
        
        conn.commit()
        return jsonify({"success": True, "revisoes_restantes": revisoes - 1})

    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

# --- NOVA ROTA: GERAR PIX MENSALIDADE ---
@app.route('/api/pay_monthly', methods=['POST'])
@login_required
def pay_monthly():
    if not mp_sdk: return jsonify({"error": "Mercado Pago Offline"}), 500
    
    data = request.json
    invoice_id = data.get('invoice_id')
    
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Valida se a fatura pertence ao usuário e está pendente
        cur.execute("SELECT id, amount, due_date FROM invoices WHERE id = %s AND client_id = %s AND status = 'pending'", (invoice_id, current_user.id))
        invoice = cur.fetchone()
        
        if not invoice:
            return jsonify({"error": "Fatura não encontrada ou já paga."}), 404

        # Cria Preferência MP
        preference_data = {
            "items": [{
                "id": f"INV-{invoice['id']}",
                "title": f"Mensalidade Leanttro - Venc: {invoice['due_date']}",
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": float(invoice['amount'])
            }],
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

        # Criação focada em PIX
        pref = mp_sdk.preference().create(preference_data)
        
        # Como o MP retorna URL de checkout, mas queremos o Copy Paste direto,
        # idealmente usaríamos a API v1/payments, mas para simplificar com o SDK Preference:
        # Retornamos o link do checkout que abre o Pix.
        # OU: Se quiser o copy-paste direto, precisamos criar um pagamento pendente.
        # Vamos retornar o init_point por enquanto, que é mais seguro com o SDK básico.
        
        return jsonify({
            "checkout_url": pref["response"]["init_point"],
            "invoice_id": invoice_id
        })

    except Exception as e:
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

# --- ROTA DE DOWNLOAD DO CONTRATO (ATUALIZADA) ---
@app.route('/api/contract/download', methods=['GET'])
@login_required
def download_contract_real():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão"}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cur.execute("""
            SELECT c.name, c.document, p.name as product_name, p.prazo_products,
                   o.total_setup, o.total_monthly
            FROM clients c
            JOIN orders o ON c.id = o.client_id
            JOIN products p ON o.product_id = p.id
            WHERE c.id = %s
            ORDER BY o.created_at DESC LIMIT 1
        """, (current_user.id,))
        
        data = cur.fetchone()
        
        if not data:
            return jsonify({"error": "Nenhum contrato ativo encontrado."}), 404

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
        p.drawString(50, y, f"CONTRATANTE: {data['name'].upper()}")
        p.drawString(50, y-20, f"DOCUMENTO: {data.get('document', 'Não informado')}")
        
        y -= 80
        p.setFont("Helvetica-Bold", 14)
        p.drawString(50, y, "ESCOPO E PRAZOS")
        p.setFont("Helvetica", 12)
        y -= 25
        p.drawString(50, y, f"Projeto: {data['product_name']}")
        
        prazo_str = extract_days(data.get('prazo_products')) or 10
        p.drawString(50, y-20, f"Entrega Estimada: {prazo_str} dias úteis")
        
        p.drawString(50, y-40, f"Setup: R$ {data['total_setup']:,.2f}")
        p.drawString(50, y-60, f"Mensal: R$ {data['total_monthly']:,.2f}")
        
        # --- ALTERAÇÃO: CLÁUSULAS CORRIGIDAS (3 REVISÕES) ---
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
        # -----------------------------------------------------
        
        p.showPage()
        p.save()
        buffer.seek(0)
        
        filename = f"Contrato_Leanttro_{current_user.id}.pdf"
        return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')
        
    except Exception as e:
        print(f"Erro PDF Real: {e}")
        return jsonify({"error": "Erro ao gerar contrato"}), 500
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

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
        
        # --- ALTERAÇÃO: CLÁUSULAS CORRIGIDAS (3 REVISÕES) ---
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
        # -----------------------------------------------------
        
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
        
        preference_data = {
            "items": [{"id": str(cart['product_id']), "title": f"PROJETO WEB #{order_id}", "quantity": 1, "unit_price": total_setup}],
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
                
                # Se for pagamento de Fatura (INV-123)
                if ref and ref.startswith('INV-'):
                    invoice_id = ref.split('-')[1]
                    if status == 'approved':
                         conn = get_db_connection()
                         cur = conn.cursor()
                         cur.execute("UPDATE invoices SET status = 'paid', paid_at = NOW() WHERE id = %s", (invoice_id,))
                         conn.commit()
                         if db_pool and conn: db_pool.putconn(conn)
                         elif conn: conn.close()
                
                # Se for pagamento de Setup (Order ID puro)
                elif status == 'approved' and ref:
                    order_id = ref
                    conn = get_db_connection()
                    cur = conn.cursor()
                    
                    cur.execute("UPDATE orders SET payment_status = 'approved' WHERE id = %s", (order_id,))
                    
                    cur.execute("""
                        UPDATE clients 
                        SET status = 'active' 
                        WHERE id = (SELECT client_id FROM orders WHERE id = %s)
                    """, (order_id,))
                    
                    conn.commit()
                    if db_pool and conn: db_pool.putconn(conn)
                    elif conn: conn.close()
                    print(f"✅ PAGAMENTO CONFIRMADO: Pedido {order_id} ativado.")
                    
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            print(f"Erro Webhook: {e}")
            return jsonify({"error": str(e)}), 500
    
    return jsonify({"status": "ignored"}), 200

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
            model = genai.GenerativeModel(SELECTED_MODEL_NAME)
            response = model.generate_content(tech_prompt_input)
            tech_prompt = response.text
        except:
            tech_prompt = "Erro ao gerar com IA."

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO briefings (client_id, colors, style_preference, site_sections, uploaded_files, ai_generated_prompt, status, revisoes_restantes)
            VALUES (%s, %s, %s, %s, %s, %s, 'ativo', 3)
        """, (current_user.id, colors, style, sections, ",".join(file_names), tech_prompt))
        conn.commit()
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

        return jsonify({"success": True, "redirect": "/admin"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- CHATBOT LELIS ---
@app.route('/api/chat', methods=['POST'])
def handle_chat():
    print(f"\n--- [LELIS] Chat trigger (Modelo ativo: {SELECTED_MODEL_NAME}) ---")
    
    if not chat_model:
        return jsonify({'error': 'Serviço de IA Offline.'}), 503

    try:
        data = request.json
        history = data.get('conversationHistory', [])
        
        gemini_history = []
        for message in history:
            role = 'user' if message['user'] == 'user' else 'model'
            gemini_history.append({'role': role, 'parts': [{'text': message['text']}]})
            
        chat_session = chat_model.start_chat(history=gemini_history)
        
        user_message = data.get('message', '') 
        if history and history[-1]['role'] == 'user':
            user_message = history[-1]['text']
        if not user_message: user_message = "Olá"

        response = chat_session.send_message(
            user_message,
            generation_config=genai.types.GenerationConfig(temperature=0.7),
            safety_settings={
                 'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE',
                 'SEXUAL' : 'BLOCK_NONE', 'DANGEROUS' : 'BLOCK_NONE'
            }
        )
        return jsonify({'reply': response.text})

    except Exception as e:
        print(f"❌ ERRO CHAT: {e}")
        return jsonify({'reply': "Minha conexão caiu... 🔌 Chama no WhatsApp?"}), 200

@app.route('/api/briefing/chat', methods=['POST'])
@login_required
def briefing_chat():
    data = request.json
    last_msg = data.get('message')
    try:
        model = genai.GenerativeModel(SELECTED_MODEL_NAME)
        response = model.generate_content(f"Você é LIA, especialista em Briefing. Ajude o cliente a definir o site. Cliente: {last_msg}")
        return jsonify({"reply": response.text})
    except:
        return jsonify({"reply": "Erro de conexão com a IA."})

# --- ROTA EMERGÊNCIA DB (ATUALIZADA) ---
@app.route('/fix-db')
def fix_db():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Garante coluna de revisões
        cur.execute("ALTER TABLE briefings ADD COLUMN IF NOT EXISTS revisoes_restantes INTEGER DEFAULT 3;")
        # Garante coluna de documento (CPF/CNPJ)
        cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS document VARCHAR(50);")
        
        # --- TABELA DE FATURAS (INVOICES) ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                client_id INTEGER REFERENCES clients(id),
                amount DECIMAL(10,2) NOT NULL,
                due_date DATE NOT NULL,
                status VARCHAR(20) DEFAULT 'pending', -- pending, paid, overdue
                created_at TIMESTAMP DEFAULT NOW(),
                paid_at TIMESTAMP
            );
        """)
        
        conn.commit()
        return "Banco Atualizado: Tabela 'invoices' e colunas verificadas."
    except Exception as e:
        return f"Erro ao atualizar DB: {e}"
    finally:
        if db_pool and conn: db_pool.putconn(conn)
        elif conn: conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)