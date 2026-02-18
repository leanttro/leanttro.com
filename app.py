from flask import Flask, render_template, request, jsonify, redirect, url_for
import requests
import os
from datetime import datetime
from dotenv import load_dotenv
import traceback

# Carrega variáveis de ambiente do arquivo .env
load_dotenv()

app = Flask(__name__)

# --- CONFIGURAÇÕES GERAIS ---
# Tenta pegar do .env, se não achar usa valores padrão seguros
DIRECTUS_URL = os.getenv("DIRECTUS_URL", "https://api2.leanttro.com").rstrip('/')
DIRECTUS_TOKEN = os.getenv("DIRECTUS_TOKEN", "") 
LOJA_ID = os.getenv("LOJA_ID", "") 

# --- CONFIGURAÇÕES SUPERFRETE ---
SUPERFRETE_TOKEN = os.getenv("SUPERFRETE_TOKEN", "")
SUPERFRETE_URL = os.getenv("SUPERFRETE_URL", "https://api.superfrete.com/api/v0/calculator")
CEP_ORIGEM = "01026000" # CEP da 25 de Março (Padrão de envio)

# --- DADOS PADRÃO (FALLBACK) ---
# Usado se o Directus falhar ou não houver loja configurada
LOJA_PADRAO = {
    "nome": "Leanttro Ecosystem",
    "logo": "https://leanttro.com/static/img/logo-placeholder.png", # Coloque um link de logo provisório se quiser
    "cor_primaria": "#7c3aed",
    "whatsapp": "5511913324827",
    "slug_url": "painel",
    "banner1": "",
    "link1": "#",
    "banner2": "",
    "link2": "#",
    "bannermenor1": "",
    "bannermenor2": ""
}

# --- TABELA DE MEDIDAS (Para cálculo de frete) ---
DIMENSOES = {
    "Pequeno": {"height": 4, "width": 12, "length": 16, "weight": 0.3},
    "Medio":   {"height": 10, "width": 20, "length": 20, "weight": 1.0},
    "Grande":  {"height": 20, "width": 30, "length": 30, "weight": 3.0}
}

# --- FUNÇÃO AUXILIAR DE IMAGEM ---
def get_img_url(image_id_or_url):
    """Converte ID do Directus em URL completa ou retorna placeholder"""
    if not image_id_or_url:
        return "" # Retorna vazio para o template tratar ou usar placeholder lá
    
    # Se for um dicionário (objeto retornado pelo Directus)
    if isinstance(image_id_or_url, dict):
        return f"{DIRECTUS_URL}/assets/{image_id_or_url.get('id')}"
    
    # Se já for URL completa
    if isinstance(image_id_or_url, str) and image_id_or_url.startswith('http'):
        return image_id_or_url
    
    # Se for apenas o ID (string)
    return f"{DIRECTUS_URL}/assets/{image_id_or_url}"

# --- HELPER: BUSCAR DADOS DA LOJA ---
def get_loja_data():
    """Busca dados no Directus ou retorna o padrão para não quebrar o site"""
    try:
        if LOJA_ID:
            headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"} if DIRECTUS_TOKEN else {}
            resp = requests.get(f"{DIRECTUS_URL}/items/lojas/{LOJA_ID}?fields=*.*", headers=headers, timeout=5)
            
            if resp.status_code == 200:
                data = resp.json().get('data', {})
                
                # Tratamento de logo
                logo_raw = data.get('logo')
                logo_final = get_img_url(logo_raw)

                return {
                    "nome": data.get('nome', LOJA_PADRAO['nome']),
                    "logo": logo_final,
                    "cor_primaria": data.get('cor_primaria', LOJA_PADRAO['cor_primaria']),
                    "whatsapp": data.get('whatsapp_comercial') or LOJA_PADRAO['whatsapp'],
                    "slug_url": data.get('slug_url', 'painel'),
                    # Banners
                    "banner1": get_img_url(data.get('bannerprincipal1')),
                    "link1": data.get('linkbannerprincipal1', '#'),
                    "banner2": get_img_url(data.get('bannerprincipal2')),
                    "link2": data.get('linkbannerprincipal2', '#'),
                    "bannermenor1": get_img_url(data.get('bannermenor1')),
                    "bannermenor2": get_img_url(data.get('bannermenor2'))
                }
    except Exception as e:
        print(f"Erro ao conectar Directus (Loja): {e}")
        traceback.print_exc()
    
    return LOJA_PADRAO

# --- HELPER: BUSCAR CATEGORIAS ---
def get_categorias():
    if not LOJA_ID: return []
    try:
        headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"} if DIRECTUS_TOKEN else {}
        url_cat = f"{DIRECTUS_URL}/items/categorias?filter[loja_id][_eq]={LOJA_ID}&filter[status][_eq]=published"
        resp_cat = requests.get(url_cat, headers=headers, timeout=5)
        if resp_cat.status_code == 200:
            return resp_cat.json().get('data', [])
    except Exception as e:
        print(f"Erro ao buscar categorias: {e}")
    return []

# --- ROTA: HOME (INDEX) - O HUB ---
@app.route('/')
def index():
    # Carrega dados da loja (ou padrão se falhar)
    loja = get_loja_data()
    
    # Busca produtos apenas se necessário (para a vitrine rápida)
    produtos = []
    try:
        if LOJA_ID:
            headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"} if DIRECTUS_TOKEN else {}
            # Busca produtos publicados e com destaque ou recentes
            url_prod = f"{DIRECTUS_URL}/items/produtos?filter[loja_id][_eq]={LOJA_ID}&filter[status][_eq]=published&limit=4"
            resp_prod = requests.get(url_prod, headers=headers, timeout=5)
            
            if resp_prod.status_code == 200:
                raw_prods = resp_prod.json().get('data', [])
                for p in raw_prods:
                    produtos.append({
                        "id": str(p['id']), 
                        "nome": p['nome'],
                        "preco": float(p['preco']) if p.get('preco') else None,
                        "imagem": get_img_url(p.get('imagem_destaque') or p.get('imagem1')),
                        "urgencia": p.get('status_urgencia', 'Normal')
                    })
    except Exception as e:
        print(f"Erro ao buscar produtos home: {e}")

    # Renderiza o index.html com os dados
    return render_template('index.html', loja=loja, produtos=produtos)

# --- ROTA: TECNOLOGIA (Landing Page Digital) ---
@app.route('/tecnologia')
def tecnologia():
    loja = get_loja_data()
    # Verifica se o template existe, se não, usa um fallback ou erro amigável
    try:
        return render_template('tecnologia.html', loja=loja)
    except Exception as e:
        print(f"Erro template tecnologia: {e}")
        return "<h1>Página em Construção</h1><p>O arquivo tecnologia.html não foi encontrado.</p>", 404

# --- ROTA: PRESENTES (Loja Online Completa) ---
@app.route('/presentes')
def presentes():
    loja = get_loja_data()
    categorias = get_categorias()
    
    # Filtros de Categoria
    cat_filter = request.args.get('categoria')
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"} if DIRECTUS_TOKEN else {}
    
    filter_str = f"&filter[loja_id][_eq]={LOJA_ID}&filter[status][_eq]=published"
    if cat_filter:
        filter_str += f"&filter[categoria_id][_eq]={cat_filter}"

    produtos = []
    try:
        url_prod = f"{DIRECTUS_URL}/items/produtos?{filter_str}"
        resp_prod = requests.get(url_prod, headers=headers, timeout=8)
        
        if resp_prod.status_code == 200:
            produtos_raw = resp_prod.json().get('data', [])
            for p in produtos_raw:
                img_url = get_img_url(p.get('imagem_destaque') or p.get('imagem1'))
                
                # Tratamento de Variantes
                variantes_tratadas = []
                if p.get('variantes'):
                    for v in p['variantes']:
                        v_img = get_img_url(v.get('foto')) if v.get('foto') else img_url
                        variantes_tratadas.append({"nome": v.get('nome', 'Padrão'), "foto": v_img})

                produtos.append({
                    "id": str(p['id']), 
                    "nome": p['nome'],
                    "slug": p.get('slug'),
                    "preco": float(p['preco']) if p.get('preco') else None,
                    "imagem": img_url,
                    "variantes": variantes_tratadas,
                    "descricao": p.get('descricao', ''),
                    "categoria_id": p.get('categoria_id')
                })
    except Exception as e:
        print(f"Erro Produtos Loja: {e}")

    # Renderiza o template da loja (pode ser o mesmo index com flag ou um 'loja.html')
    # Assumindo que você usa index.html para tudo, passamos uma flag 'modo_loja'
    return render_template('index.html', loja=loja, categorias=categorias, produtos=produtos, modo_loja=True)

# --- ROTA: IDENTIDADE DIGITAL / QR CODE ---
@app.route('/qrcodebrindes')
def qrcode():
    loja = get_loja_data()
    return render_template('index.html', loja=loja, qrcode_mode=True) # Pode criar um template específico depois

# --- ROTA: PRODUTO DETALHE ---
@app.route('/produto/<slug>')
def produto(slug):
    # Por enquanto retorna simples para não quebrar se não tiver template
    return f"<h1>Detalhe do Produto: {slug}</h1><p>Em desenvolvimento...</p>"

# --- ROTA: API FRETE (CÁLCULO) ---
@app.route('/api/calcular-frete', methods=['POST'])
def calcular_frete():
    data = request.json
    cep_destino = data.get('cep')
    itens_carrinho = data.get('itens')

    if not cep_destino or not itens_carrinho:
        return jsonify({"erro": "Dados inválidos"}), 400

    peso_total = 0.0
    altura_total = 0.0
    largura_max = 0.0
    comprimento_max = 0.0
    valor_seguro = 0.0

    # Calcula dimensões do pacote baseado nos itens
    for item in itens_carrinho:
        classe = item.get('classe_frete', 'Pequeno')
        qtd = int(item.get('qtd', 1))
        medidas = DIMENSOES.get(classe, DIMENSOES['Pequeno'])
        
        peso_total += medidas['weight'] * qtd
        altura_total += medidas['height'] * qtd 
        largura_max = max(largura_max, medidas['width'])
        comprimento_max = max(comprimento_max, medidas['length'])
        
        if item.get('preco'): 
            valor_seguro += float(item['preco']) * qtd

    # Limites mínimos dos Correios
    altura_total = max(altura_total, 2)
    largura_max = max(largura_max, 11)
    comprimento_max = max(comprimento_max, 16)
    peso_total = max(peso_total, 0.3)
    valor_seguro = max(valor_seguro, 25.00)

    headers = {
        "Authorization": f"Bearer {SUPERFRETE_TOKEN}",
        "User-Agent": "Leanttro Store",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "from": { "postal_code": CEP_ORIGEM },
        "to": { "postal_code": cep_destino },
        "services": "PAC,SEDEX",
        "options": { "own_hand": False, "receipt": False, "insurance_value": valor_seguro },
        "package": { "height": int(altura_total), "width": int(largura_max), "length": int(comprimento_max), "weight": peso_total }
    }

    try:
        response = requests.post(SUPERFRETE_URL, json=payload, headers=headers, timeout=10)
        cotacoes = response.json()
        opcoes = []
        
        # Tratamento do retorno da API (que as vezes muda formato)
        lista_retorno = []
        if isinstance(cotacoes, list):
            lista_retorno = cotacoes
        elif isinstance(cotacoes, dict):
             lista_retorno = cotacoes.get('shipping_options', [])
             if not lista_retorno and 'id' in cotacoes: lista_retorno = [cotacoes] # Retorno único

        for c in lista_retorno:
            if isinstance(c, dict) and ('error' in c and c['error']): continue
            
            nome = c.get('name') or c.get('service', {}).get('name') or 'Entrega'
            preco = c.get('price') or c.get('custom_price') or c.get('vlrFrete')
            prazo = c.get('delivery_time') or c.get('days') or c.get('prazoEnt')

            if preco:
                opcoes.append({
                    "servico": nome,
                    "transportadora": "Correios", 
                    "preco": float(preco) + 4.00, # Taxa de manuseio opcional
                    "prazo": int(prazo) + 2 # Margem de segurança
                })

        opcoes.sort(key=lambda x: x['preco'])
        return jsonify(opcoes)

    except Exception as e:
        print(f"Erro Frete: {e}")
        traceback.print_exc()
        return jsonify({"erro": "Erro ao calcular frete", "msg": str(e)}), 500

if __name__ == '__main__':
    # Roda na porta 5000 (Padrão Flask)
    app.run(debug=True, host='0.0.0.0', port=5000)