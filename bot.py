import io
import os
import html
import random
import asyncio
import psycopg2
from datetime import date
from aiohttp import web
from PIL import Image, ImageOps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ==========================================
# CONFIGURAÇÕES DO ECCO-MIX TRICK
# ==========================================
TOKEN = os.environ["BOT_TOKEN"].strip()
DATABASE_URL = os.environ["DATABASE_URL"].strip()
WEBHOOK_URL = os.environ["WEBHOOK_URL"].strip().rstrip("/")
PORT = int(os.environ.get("PORT", 8080))

SUPER_ADMINS = [8622687849]
env_admins = os.environ.get("ADMIN_IDS", "")
if env_admins:
    SUPER_ADMINS.extend([int(x.strip()) for x in env_admins.split(",") if x.strip().isdigit()])

GIROS_DIARIOS = 10
LARGURA_CARTA = 900
ALTURA_CARTA = 1200

TIERS_CONFIG = {
    1: {"nome": "Tier 1 (Comum)", "curto": "Tier 1", "emoji": "🌱", "peso": 48},
    2: {"nome": "Tier 2 (Incomum)", "curto": "Tier 2", "emoji": "💧", "peso": 27},
    3: {"nome": "Tier 3 (Raro)", "curto": "Tier 3", "emoji": "🦋", "peso": 15},
    4: {"nome": "Tier 4 (Épico)", "curto": "Tier 4", "emoji": "🔮", "peso": 7},
    5: {"nome": "Tier 5 (Lendário)", "curto": "Tier 5", "emoji": "💠", "peso": 3},
}

CATALOGO_INICIAL = {
    "Animes": [],
    "Séries": [],
    "Filmes": [],
    "Música": [
        "Alt Bands",
        "Emo Bands",
        "Underground Rap",
        "Brazilian Underground",
        "Alt / Underground",
        "Hyperpop",
        "EDM",
        "K-pop",
    ],
    "Brainrots": [],
}

# Estados do assistente /addcarta
(
    ST_CAT,
    ST_NEW_CAT,
    ST_ASK_SUB_CHOICE,
    ST_SUB,
    ST_NEW_SUB,
    ST_NOME,
    ST_NEW_NOME,
    ST_MIDIA,
    ST_TIER,
) = range(9)

def esc(texto: str) -> str:
    return html.escape(str(texto), quote=False)

# ==========================================
# BANCO DE DADOS E INICIALIZAÇÃO
# ==========================================
def get_db():
    return psycopg2.connect(DATABASE_URL)

def iniciar_banco():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS categorias (
            id SERIAL PRIMARY KEY,
            nome TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subcategorias (
            id SERIAL PRIMARY KEY,
            categoria_id INTEGER NOT NULL REFERENCES categorias(id) ON DELETE CASCADE,
            nome TEXT NOT NULL,
            UNIQUE(categoria_id, nome)
        );
        CREATE TABLE IF NOT EXISTS molduras (
            tier INTEGER PRIMARY KEY CHECK (tier BETWEEN 1 AND 5),
            png_bytes BYTEA NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            nome TEXT,
            apelido TEXT,
            giros_restantes INTEGER DEFAULT 10,
            ultimo_reset TEXT
        );
        ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS apelido TEXT;
        CREATE TABLE IF NOT EXISTS cartas (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            categoria_id INTEGER NOT NULL REFERENCES categorias(id),
            subcategoria_id INTEGER REFERENCES subcategorias(id),
            tier INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 5),
            peso_drop INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'photo'
        );
        CREATE TABLE IF NOT EXISTS inventario (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES usuarios(user_id),
            carta_id INTEGER REFERENCES cartas(id) ON DELETE CASCADE,
            quantidade INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS trocas (
            id SERIAL PRIMARY KEY,
            remetente_id BIGINT NOT NULL,
            destinatario_id BIGINT NOT NULL,
            carta_oferecida_id INTEGER NOT NULL,
            carta_pedida_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pendente'
        );
    """)

    cur.execute("SELECT COUNT(*) FROM categorias")
    if cur.fetchone()[0] == 0:
        for cat_nome, subs in CATALOGO_INICIAL.items():
            cur.execute("INSERT INTO categorias (nome) VALUES (%s) RETURNING id", (cat_nome,))
            cat_id = cur.fetchone()[0]
            for sub_nome in subs:
                cur.execute(
                    "INSERT INTO subcategorias (categoria_id, nome) VALUES (%s, %s)",
                    (cat_id, sub_nome)
                )

    conn.commit()
    cur.close()
    conn.close()

def eh_admin(user_id: int) -> bool:
    if user_id in SUPER_ADMINS:
        return True
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id = %s", (user_id,))
    achou = cur.fetchone() is not None
    cur.close()
    conn.close()
    return achou

def verificar_usuario(user_id: int, username: str = "") -> int:
    conn = get_db()
    cur = conn.cursor()
    hoje = str(date.today())
    username_limpo = (username or "").lstrip("@").lower()

    cur.execute("SELECT giros_restantes, ultimo_reset FROM usuarios WHERE user_id = %s", (user_id,))
    row = cur.fetchone()

    if not row:
        cur.execute(
            "INSERT INTO usuarios (user_id, username, giros_restantes, ultimo_reset) VALUES (%s, %s, %s, %s)",
            (user_id, username_limpo, GIROS_DIARIOS, hoje)
        )
        giros = GIROS_DIARIOS
    else:
        giros, ultimo_reset = row
        if username_limpo:
            cur.execute("UPDATE usuarios SET username = %s WHERE user_id = %s", (username_limpo, user_id))
        if ultimo_reset != hoje:
            giros = GIROS_DIARIOS
            cur.execute(
                "UPDATE usuarios SET giros_restantes = %s, ultimo_reset = %s WHERE user_id = %s",
                (GIROS_DIARIOS, hoje, user_id)
            )

    conn.commit()
    cur.close()
    conn.close()
    return giros

def obter_apelido(user_id: int, username: str = "") -> tuple[str, bool]:
    """Retorna (nome_exibicao, tem_apelido_personalizado). Nunca usa o nome real do Telegram."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT apelido, username FROM usuarios WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row:
        apelido_db, uname_db = row
        if apelido_db and apelido_db.strip():
            return apelido_db.strip(), True
        uname = (username or uname_db or "").lstrip("@")
        if uname:
            return f"@{uname}", False

    uname_fallback = (username or "").lstrip("@")
    if uname_fallback:
        return f"@{uname_fallback}", False
    return f"Mixer #{str(user_id)[-4:]}", False

def processar_imagem_carta(image_bytes: bytes, tier: int) -> io.BytesIO:
    """Padroniza qualquer foto em 1080x1700 e aplica a moldura PNG do tier se existir."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    base = ImageOps.fit(img, (LARGURA_CARTA, ALTURA_CARTA), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT png_bytes FROM molduras WHERE tier = %s", (tier,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row and row[0]:
        moldura = Image.open(io.BytesIO(bytes(row[0]))).convert("RGBA")
        if moldura.size != (LARGURA_CARTA, ALTURA_CARTA):
            moldura = moldura.resize((LARGURA_CARTA, ALTURA_CARTA), Image.Resampling.LANCZOS)
        final_img = Image.alpha_composite(base, moldura)
    else:
        final_img = base

    saida = io.BytesIO()
    final_img.convert("RGB").save(saida, format="JPEG", quality=92)
    saida.seek(0)
    return saida

async def enviar_midia_carta(message, file_id: str, media_type: str, legenda: str):
    if media_type == "animation":
        await message.reply_animation(animation=file_id, caption=legenda, parse_mode="HTML")
    else:
        await message.reply_photo(photo=file_id, caption=legenda, parse_mode="HTML")

# ==========================================
# COMANDOS DE JOGADORES COMUNS
# ==========================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    giros = verificar_usuario(user.id, user.username)
    apelido, tem_apelido = obter_apelido(user.id, user.username)

    texto = (
        f"🌐 <b>Ecco-Mix Trick</b>\n"
        f"🫧 Jogador: <b>{esc(apelido)}</b>\n"
        f"🍃 Giros disponíveis hoje: <b>{giros}</b>\n\n"
        f"<b>Comandos disponíveis:</b>\n"
        f"• /girar — Escolher uma categoria e sortear uma carta\n"
        f"• /apelido <code>nome</code> — Definir seu nome de exibição no jogo\n"
        f"• /colecao — Ver toda a sua coleção agrupada\n"
        f"• /artista <code>nome</code> — Ver seu progresso nos 5 tiers de um artista/personagem\n"
        f"• /carta <code>ID</code> — Visualizar a imagem/GIF de uma carta\n"
        f"• /trocar <code>minha_id</code> <code>id_dele</code> — Propor troca (responda à mensagem da pessoa)\n"
        f"• /meuid — Ver seu ID numérico do Telegram"
    )
    if not tem_apelido:
        texto += "\n\n💡 <i>Dica: use <code>/apelido SeuNome</code> para personalizar como seu nome aparece nas cartas.</i>"
    if eh_admin(user.id):
        texto += "\n\n🛡️ <i>Modo Studio ativo: /studio</i>"

    await update.message.reply_text(texto, parse_mode="HTML")

async def cmd_apelido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    verificar_usuario(user.id, user.username)

    novo_apelido = " ".join(context.args).strip()
    if not novo_apelido:
        atual, _ = obter_apelido(user.id, user.username)
        await update.message.reply_text(
            f"🫧 Seu nome atual no jogo é: <b>{esc(atual)}</b>\n\n"
            f"Para alterar, envie: <code>/apelido Seu Novo Nome</code>",
            parse_mode="HTML"
        )
        return

    if len(novo_apelido) < 2 or len(novo_apelido) > 32:
        await update.message.reply_text("⚠️ O apelido precisa ter entre <b>2 e 32</b> caracteres.", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE usuarios SET apelido = %s WHERE user_id = %s", (novo_apelido, user.id))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        f"✅ Apelido atualizado para: 🫧 <b>{esc(novo_apelido)}</b>",
        parse_mode="HTML"
    )

async def cmd_meu_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Seu ID numérico é: <code>{update.effective_user.id}</code>", parse_mode="HTML")

async def cmd_girar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    giros = verificar_usuario(user.id, user.username)

    if giros <= 0:
        await update.message.reply_text("⏳ Seus giros acabaram por hoje! Volte amanhã.")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM categorias ORDER BY id ASC")
    categorias = cur.fetchall()
    cur.close()
    conn.close()

    botoes = []
    linha = []
    for cat_id, cat_nome in categorias:
        linha.append(InlineKeyboardButton(cat_nome, callback_data=f"rcat|{cat_id}|{user.id}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("🌐 Mix Aleatório (Tudo)", callback_data=f"rcat|all|{user.id}")])

    await update.message.reply_text(
        f"🍃 Giros restantes: <b>{giros}</b>\nEscolha a categoria:",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )

async def callback_girar_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, cat_alvo, dono_id = query.data.split("|")

    if query.from_user.id != int(dono_id):
        await query.answer("Esse menu de giro não é seu!", show_alert=True)
        return

    if cat_alvo == "all":
        await executar_sorteio(query, cat_id=None, sub_id=None)
        return

    cat_id = int(cat_alvo)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM subcategorias WHERE categoria_id = %s ORDER BY id ASC", (cat_id,))
    subs = cur.fetchall()
    cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
    cat_row = cur.fetchone()
    cur.close()
    conn.close()

    if not cat_row:
        await query.answer("Categoria não encontrada.", show_alert=True)
        return

    if not subs:
        await executar_sorteio(query, cat_id=cat_id, sub_id=None)
        return

    botoes = []
    linha = []
    for sub_id, sub_nome in subs:
        linha.append(InlineKeyboardButton(sub_nome, callback_data=f"rsub|{cat_id}|{sub_id}|{dono_id}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton(f"🌐 Todos de {cat_row[0]}", callback_data=f"rsub|{cat_id}|all|{dono_id}")])

    await query.answer()
    await query.edit_message_text(
        f"💿 <b>{esc(cat_row[0])}</b> — Escolha a categoria:",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )

async def callback_girar_subcategoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, cat_id_str, sub_alvo, dono_id = query.data.split("|")

    if query.from_user.id != int(dono_id):
        await query.answer("Esse menu de giro não é seu!", show_alert=True)
        return

    cat_id = int(cat_id_str)
    sub_id = None if sub_alvo == "all" else int(sub_alvo)
    await executar_sorteio(query, cat_id=cat_id, sub_id=sub_id)

async def executar_sorteio(query, cat_id=None, sub_id=None):
    user = query.from_user
    giros = verificar_usuario(user.id, user.username)
    if giros <= 0:
        await query.answer("Seus giros acabaram!", show_alert=True)
        return

    conn = get_db()
    cur = conn.cursor()

    sql_base = """
        SELECT c.id, c.nome, cat.id, cat.nome, sub.id, sub.nome, c.tier, c.peso_drop, c.file_id, c.media_type
        FROM cartas c
        JOIN categorias cat ON c.categoria_id = cat.id
        LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
    """
    if cat_id is None:
        cur.execute(sql_base)
    elif sub_id is None:
        cur.execute(sql_base + " WHERE c.categoria_id = %s", (cat_id,))
    else:
        cur.execute(sql_base + " WHERE c.categoria_id = %s AND c.subcategoria_id = %s", (cat_id, sub_id))

    cartas = cur.fetchall()
    if not cartas:
        await query.answer("Ainda não há cartas cadastradas aqui.", show_alert=True)
        cur.close()
        conn.close()
        return

    pesos = [c[7] for c in cartas]
    (
        carta_id,
        nome_carta,
        cid_cat,
        nome_cat,
        cid_sub,
        nome_sub,
        tier,
        _,
        file_id,
        media_type,
    ) = random.choices(cartas, weights=pesos, k=1)[0]

    cur.execute("UPDATE usuarios SET giros_restantes = giros_restantes - 1 WHERE user_id = %s", (user.id,))
    cur.execute("SELECT id, quantidade FROM inventario WHERE user_id = %s AND carta_id = %s", (user.id, carta_id))
    item = cur.fetchone()

    if item:
        qtd_carta = item[1] + 1
        cur.execute("UPDATE inventario SET quantidade = %s WHERE id = %s", (qtd_carta, item[0]))
    else:
        qtd_carta = 1
        cur.execute("INSERT INTO inventario (user_id, carta_id, quantidade) VALUES (%s, %s, 1)", (user.id, carta_id))

    # Quantas cartas diferentes desse personagem/artista o jogador possui vs total cadastrado
    cur.execute("""
        SELECT COUNT(DISTINCT c.tier)
        FROM inventario i
        JOIN cartas c ON i.carta_id = c.id
        WHERE i.user_id = %s AND LOWER(c.nome) = LOWER(%s)
    """, (user.id, nome_carta))
    tiers_obtidos = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM cartas WHERE LOWER(nome) = LOWER(%s)", (nome_carta,))
    tiers_cadastrados = cur.fetchone()[0]

    conn.commit()
    cur.close()
    conn.close()
    await query.answer()

    apelido_user, _ = obter_apelido(user.id, user.username)
    tier_info = TIERS_CONFIG[tier]
    cat_codigo = cid_sub if cid_sub else cid_cat
    local_str = f"{esc(nome_cat)} • {esc(nome_sub)}" if nome_sub else esc(nome_cat)

    legenda = (
        f"🌐 <b>MIX SINTONIZADO! Você desbloqueou:</b>\n\n"
        f"{tier_info['emoji']} <b>{carta_id}. {esc(nome_carta)}.</b> <i>[{tier_info['curto']}]</i>\n"
        f"💿 <b>{cat_codigo}. {local_str}</b>\n\n"
        f"🫧 <b>{esc(apelido_user)}.</b> ({qtd_carta}x) ({tiers_obtidos}/{tiers_cadastrados})\n"
        f"🌿 <i>Giros restantes: <b>{giros - 1}</b></i>"
    )
    await enviar_midia_carta(query.message, file_id, media_type, legenda)

async def cmd_artista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    verificar_usuario(user.id, user.username)

    busca = " ".join(context.args).strip()
    if not busca:
        await update.message.reply_text("Uso: <code>/artista nome</code> (ex: <code>/artista kuru</code>)", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT c.id, c.nome, cat.nome, sub.nome, c.tier
        FROM cartas c
        JOIN categorias cat ON c.categoria_id = cat.id
        LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
        WHERE LOWER(c.nome) = LOWER(%s)
        ORDER BY c.tier ASC
    """, (busca,))
    cartas = cur.fetchall()

    if not cartas:
        cur.execute("""
            SELECT c.id, c.nome, cat.nome, sub.nome, c.tier
            FROM cartas c
            JOIN categorias cat ON c.categoria_id = cat.id
            LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
            WHERE LOWER(c.nome) LIKE LOWER(%s)
            ORDER BY c.nome ASC, c.tier ASC
        """, (f"%{busca}%",))
        resultados = cur.fetchall()
        if not resultados:
            await update.message.reply_text("Nenhum artista/personagem encontrado com esse nome.")
            cur.close()
            conn.close()
            return
        nome_alvo = resultados[0][1].lower()
        cartas = [r for r in resultados if r[1].lower() == nome_alvo]

    nome_oficial = cartas[0][1]
    cat_nome = cartas[0][2]
    sub_nome = cartas[0][3]
    mapa_tiers = {c[4]: c[0] for c in cartas}

    cur.execute("""
        SELECT c.tier, i.quantidade
        FROM inventario i
        JOIN cartas c ON i.carta_id = c.id
        WHERE i.user_id = %s AND LOWER(c.nome) = LOWER(%s)
    """, (user.id, nome_oficial))
    posse = dict(cur.fetchall())
    cur.close()
    conn.close()

    local_str = f"{esc(cat_nome)} • {esc(sub_nome)}" if sub_nome else esc(cat_nome)
    obtidas = len(posse)
    total = len(mapa_tiers)

    linhas = [
        f"🎧 <b>{esc(nome_oficial)}</b> <i>({local_str})</i>",
        f"💿 <b>Progresso:</b> {obtidas}/{total} cartas\n"
    ]

    for t in range(1, 6):
        t_cfg = TIERS_CONFIG[t]
        if t in mapa_tiers:
            cid = mapa_tiers[t]
            if t in posse:
                linhas.append(f"{t_cfg['emoji']} <b>{t_cfg['nome']}:</b> ✅ (<code>#{cid}</code> — {posse[t]}x)")
            else:
                linhas.append(f"{t_cfg['emoji']} <b>{t_cfg['nome']}:</b> ❌ (<code>#{cid}</code>)")
        else:
            linhas.append(f"{t_cfg['emoji']} <b>{t_cfg['nome']}:</b> ➖ <i>(Não cadastrada)</i>")

    await update.message.reply_text("\n".join(linhas), parse_mode="HTML")

async def cmd_colecao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    verificar_usuario(user.id, user.username)
    apelido_user, _ = obter_apelido(user.id, user.username)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome, c.tier, i.quantidade
        FROM inventario i
        JOIN cartas c ON i.carta_id = c.id
        WHERE i.user_id = %s
        ORDER BY c.nome ASC, c.tier ASC
    """, (user.id,))
    itens = cur.fetchall()

    if not itens:
        await update.message.reply_text("Sua coleção está vazia. Use /girar para pegar sua primeira carta!")
        cur.close()
        conn.close()
        return

    cur.execute("SELECT LOWER(nome), COUNT(*) FROM cartas GROUP BY LOWER(nome)")
    totais = dict(cur.fetchall())
    cur.close()
    conn.close()

    agrupado = {}
    for cid, nome, tier, qtd in itens:
        agrupado.setdefault(nome, []).append((cid, tier, qtd))

    linhas = [f"🫧 <b>Coleção de {esc(apelido_user)}</b>\n"]
    for nome, lista in agrupado.items():
        total_existentes = totais.get(nome.lower(), len(lista))
        selo = "💠 " if len(lista) == 5 else ""
        resumo_tiers = ", ".join([f"T{t} (<code>#{cid}</code> x{qtd})" for cid, t, qtd in lista])
        linhas.append(f"{selo}<b>{esc(nome)}</b> [{len(lista)}/{total_existentes}]: {resumo_tiers}")

    linhas.append("\n<i>Use /artista nome para ver detalhes por tier.</i>")
    await update.message.reply_text("\n".join(linhas)[:4000], parse_mode="HTML")

async def cmd_carta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        carta_id = int(context.args[0].replace("#", ""))
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: <code>/carta ID</code> (ex: <code>/carta 5</code>)", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome, cat.id, cat.nome, sub.id, sub.nome, c.tier, c.file_id, c.media_type
        FROM cartas c
        JOIN categorias cat ON c.categoria_id = cat.id
        LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
        WHERE c.id = %s
    """, (carta_id,))
    carta = cur.fetchone()
    cur.close()
    conn.close()

    if not carta:
        await update.message.reply_text("Carta não encontrada.")
        return

    cid, nome, cid_cat, nome_cat, cid_sub, nome_sub, tier, file_id, media_type = carta
    t_cfg = TIERS_CONFIG[tier]
    cat_codigo = cid_sub if cid_sub else cid_cat
    local_str = f"{esc(nome_cat)} • {esc(nome_sub)}" if nome_sub else esc(nome_cat)
    legenda = (
        f"{t_cfg['emoji']} <b>{cid}. {esc(nome)}.</b>\n"
        f"💿 <b>{cat_codigo}. {local_str}</b>\n"
        f"🔮 <b>Raridade:</b> {t_cfg['nome']}"
    )
    await enviar_midia_carta(update.message, file_id, media_type, legenda)

# ==========================================
# SISTEMA DE TROCAS (JOGADORES)
# ==========================================
async def cmd_trocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remetente = update.effective_user
    verificar_usuario(remetente.id, remetente.username)
    rem_apelido, _ = obter_apelido(remetente.id, remetente.username)

    destinatario_id = None
    destinatario_apelido = None

    if update.message.reply_to_message and len(context.args) >= 2:
        alvo = update.message.reply_to_message.from_user
        if alvo.is_bot or alvo.id == remetente.id:
            await update.message.reply_text("Responda à mensagem de outro jogador válido.")
            return
        destinatario_id = alvo.id
        verificar_usuario(destinatario_id, alvo.username)
        destinatario_apelido, _ = obter_apelido(destinatario_id, alvo.username)
        id_str_1, id_str_2 = context.args[0], context.args[1]
    elif len(context.args) >= 3 and context.args[0].startswith("@"):
        uname = context.args[0].lstrip("@").lower()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM usuarios WHERE username = %s", (uname,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            await update.message.reply_text("Jogador não encontrado. Peça para ele usar /girar primeiro ou responda uma mensagem dele.")
            return
        destinatario_id = row[0]
        if destinatario_id == remetente.id:
            await update.message.reply_text("Você não pode trocar cartas consigo mesmo.")
            return
        destinatario_apelido, _ = obter_apelido(destinatario_id, uname)
        id_str_1, id_str_2 = context.args[1], context.args[2]
    else:
        await update.message.reply_text(
            "Uso:\n"
            "• Responda à mensagem da pessoa: <code>/trocar SUA_ID ID_DELE</code>\n"
            "• Ou use: <code>/trocar @usuario SUA_ID ID_DELE</code>",
            parse_mode="HTML"
        )
        return

    try:
        c_oferecida = int(id_str_1.replace("#", ""))
        c_pedida = int(id_str_2.replace("#", ""))
    except ValueError:
        await update.message.reply_text("Os IDs das cartas devem ser números.")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT quantidade FROM inventario WHERE user_id = %s AND carta_id = %s", (remetente.id, c_oferecida))
    p1 = cur.fetchone()
    cur.execute("SELECT quantidade FROM inventario WHERE user_id = %s AND carta_id = %s", (destinatario_id, c_pedida))
    p2 = cur.fetchone()

    if not p1 or p1[0] < 1:
        await update.message.reply_text(f"Você não possui a carta <code>#{c_oferecida}</code>.", parse_mode="HTML")
        cur.close()
        conn.close()
        return
    if not p2 or p2[0] < 1:
        await update.message.reply_text(f"{esc(destinatario_apelido)} não possui a carta <code>#{c_pedida}</code>.", parse_mode="HTML")
        cur.close()
        conn.close()
        return

    cur.execute("SELECT nome, tier FROM cartas WHERE id = %s", (c_oferecida,))
    info1 = cur.fetchone()
    cur.execute("SELECT nome, tier FROM cartas WHERE id = %s", (c_pedida,))
    info2 = cur.fetchone()

    cur.execute(
        "INSERT INTO trocas (remetente_id, destinatario_id, carta_oferecida_id, carta_pedida_id) VALUES (%s, %s, %s, %s) RETURNING id",
        (remetente.id, destinatario_id, c_oferecida, c_pedida)
    )
    troca_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    botoes = [[
        InlineKeyboardButton("✅ Aceitar Troca", callback_data=f"trade|aceitar|{troca_id}"),
        InlineKeyboardButton("❌ Recusar", callback_data=f"trade|recusar|{troca_id}")
    ]]
    texto = (
        f"🔄 <b>Proposta de Troca #{troca_id}</b>\n\n"
        f"🫧 <b>{esc(rem_apelido)}</b> oferece:\n"
        f"   💿 <code>#{c_oferecida}</code> — {esc(info1[0])} (Tier {info1[1]})\n\n"
        f"🫧 Para <b>{esc(destinatario_apelido)}</b> em troca de:\n"
        f"   💿 <code>#{c_pedida}</code> — {esc(info2[0])} (Tier {info2[1]})"
    )
    await update.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")

async def callback_troca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, acao, troca_id_str = query.data.split("|")
    troca_id = int(troca_id_str)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT remetente_id, destinatario_id, carta_oferecida_id, carta_pedida_id, status FROM trocas WHERE id = %s", (troca_id,))
    troca = cur.fetchone()

    if not troca or troca[4] != "pendente":
        await query.answer("Essa troca já foi encerrada.", show_alert=True)
        cur.close()
        conn.close()
        return

    rem_id, dest_id, c_oferecida, c_pedida, _ = troca

    if acao == "recusar":
        if query.from_user.id not in (rem_id, dest_id):
            await query.answer("Apenas os envolvidos podem cancelar.", show_alert=True)
            cur.close()
            conn.close()
            return
        cur.execute("UPDATE trocas SET status = 'recusada' WHERE id = %s", (troca_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Troca recusada.")
        await query.edit_message_text(f"❌ Troca #{troca_id} recusada.")
        return

    if query.from_user.id != dest_id:
        await query.answer("Apenas quem recebeu a proposta pode aceitar!", show_alert=True)
        cur.close()
        conn.close()
        return

    cur.execute("SELECT id, quantidade FROM inventario WHERE user_id = %s AND carta_id = %s", (rem_id, c_oferecida))
    inv_rem = cur.fetchone()
    cur.execute("SELECT id, quantidade FROM inventario WHERE user_id = %s AND carta_id = %s", (dest_id, c_pedida))
    inv_dest = cur.fetchone()

    if not inv_rem or inv_rem[1] < 1 or not inv_dest or inv_dest[1] < 1:
        cur.execute("UPDATE trocas SET status = 'cancelada' WHERE id = %s", (troca_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Falta uma das cartas no inventário!", show_alert=True)
        await query.edit_message_text(f"⚠️ Troca #{troca_id} cancelada por falta de cartas.")
        return

    def transferir(para_user, carta_id, inv_row):
        if inv_row[1] == 1:
            cur.execute("DELETE FROM inventario WHERE id = %s", (inv_row[0],))
        else:
            cur.execute("UPDATE inventario SET quantidade = quantidade - 1 WHERE id = %s", (inv_row[0],))

        cur.execute("SELECT id FROM inventario WHERE user_id = %s AND carta_id = %s", (para_user, carta_id))
        dest_row = cur.fetchone()
        if dest_row:
            cur.execute("UPDATE inventario SET quantidade = quantidade + 1 WHERE id = %s", (dest_row[0],))
        else:
            cur.execute("INSERT INTO inventario (user_id, carta_id, quantidade) VALUES (%s, %s, 1)", (para_user, carta_id))

    transferir(dest_id, c_oferecida, inv_rem)
    transferir(rem_id, c_pedida, inv_dest)
    cur.execute("UPDATE trocas SET status = 'aceita' WHERE id = %s", (troca_id,))
    conn.commit()
    cur.close()
    conn.close()

    await query.answer("Troca concluída!")
    await query.edit_message_text(f"✅ <b>Troca #{troca_id} concluída!</b>", parse_mode="HTML")

# ==========================================
# ASSISTENTE INTERATIVO /addcarta (ESTILO @STICKERS)
# ==========================================
def montar_teclado_categorias():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM categorias ORDER BY id ASC")
    cats = cur.fetchall()
    cur.close()
    conn.close()

    botoes = []
    linha = []
    for cid, cnome in cats:
        linha.append(InlineKeyboardButton(cnome, callback_data=f"wiz_cat|{cid}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("➕ Criar nova categoria", callback_data="wiz_new_cat")])
    botoes.append([InlineKeyboardButton("❌ Cancelar", callback_data="wiz_cancel")])
    return InlineKeyboardMarkup(botoes)

def montar_teclado_subcategorias(cat_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM subcategorias WHERE categoria_id = %s ORDER BY id ASC", (cat_id,))
    subs = cur.fetchall()
    cur.close()
    conn.close()

    botoes = []
    linha = []
    for sid, snome in subs:
        linha.append(InlineKeyboardButton(snome, callback_data=f"wiz_sub|{sid}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("➕ Criar nova subcategoria", callback_data="wiz_new_sub")])
    botoes.append([InlineKeyboardButton("❌ Cancelar", callback_data="wiz_cancel")])
    return InlineKeyboardMarkup(botoes)

def montar_teclado_nomes(context: ContextTypes.DEFAULT_TYPE):
    cat_id = context.user_data["cat_id"]
    sub_id = context.user_data.get("sub_id")

    conn = get_db()
    cur = conn.cursor()
    if sub_id is None:
        cur.execute("""
            SELECT MIN(nome), COUNT(*)
            FROM cartas
            WHERE categoria_id = %s AND subcategoria_id IS NULL
            GROUP BY LOWER(nome)
            ORDER BY MIN(nome) ASC
        """, (cat_id,))
    else:
        cur.execute("""
            SELECT MIN(nome), COUNT(*)
            FROM cartas
            WHERE categoria_id = %s AND subcategoria_id = %s
            GROUP BY LOWER(nome)
            ORDER BY MIN(nome) ASC
        """, (cat_id, sub_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    context.user_data["nomes_lista"] = [r[0] for r in rows]

    botoes = []
    linha = []
    for idx, (nome_art, qtd_tiers) in enumerate(rows):
        selo = "✅" if qtd_tiers >= 5 else f"{qtd_tiers}/5"
        linha.append(InlineKeyboardButton(f"{nome_art} ({selo})", callback_data=f"wiz_nome|{idx}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("➕ Criar novo nome", callback_data="wiz_new_nome")])
    botoes.append([InlineKeyboardButton("❌ Cancelar", callback_data="wiz_cancel")])
    return InlineKeyboardMarkup(botoes)

async def wiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update.effective_user.id):
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["admin_id"] = update.effective_user.id

    await update.message.reply_text(
        "💿 <b>Nova Carta — Passo 1/5</b>\n\nEscolha a <b>categoria</b> ou crie uma nova:",
        reply_markup=montar_teclado_categorias(),
        parse_mode="HTML"
    )
    return ST_CAT

async def wiz_escolher_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ST_CAT

    await query.answer()
    if query.data == "wiz_new_cat":
        await query.edit_message_text("✏️ Digite o <b>nome da nova categoria</b>:", parse_mode="HTML")
        return ST_NEW_CAT

    cat_id = int(query.data.split("|")[1])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
    cat_nome = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM subcategorias WHERE categoria_id = %s", (cat_id,))
    tem_subs = cur.fetchone()[0] > 0
    cur.close()
    conn.close()

    context.user_data["cat_id"] = cat_id
    context.user_data["cat_nome"] = cat_nome
    context.user_data["sub_id"] = None
    context.user_data["sub_nome"] = None

    if tem_subs:
        await query.edit_message_text(
            f"📂 <b>Categoria:</b> {esc(cat_nome)}\n\n"
            f"<b>Passo 2/5</b> — Escolha a <b>subcategoria</b> ou crie uma nova:",
            reply_markup=montar_teclado_subcategorias(cat_id),
            parse_mode="HTML"
        )
        return ST_SUB

    await query.edit_message_text(
        f"📂 <b>Categoria:</b> {esc(cat_nome)}\n\n"
        f"<b>Passo 3/5</b> — Selecione um nome já cadastrado ou clique em <b>Criar novo nome</b>:",
        reply_markup=montar_teclado_nomes(context),
        parse_mode="HTML"
    )
    return ST_NOME

async def wiz_receber_nova_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ST_NEW_CAT

    nome_cat = update.message.text.strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM categorias WHERE LOWER(nome) = LOWER(%s)", (nome_cat,))
    existente = cur.fetchone()
    if existente:
        cat_id, cat_oficial = existente
    else:
        cur.execute("INSERT INTO categorias (nome) VALUES (%s) RETURNING id", (nome_cat,))
        cat_id = cur.fetchone()[0]
        cat_oficial = nome_cat
        conn.commit()
    cur.close()
    conn.close()

    context.user_data["cat_id"] = cat_id
    context.user_data["cat_nome"] = cat_oficial
    context.user_data["sub_id"] = None
    context.user_data["sub_nome"] = None

    botoes = [
        [InlineKeyboardButton("➕ Criar subcategoria", callback_data="wiz_sub_sim")],
        [InlineKeyboardButton("➡️ Sem subcategoria (ir para nomes)", callback_data="wiz_sub_nao")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="wiz_cancel")],
    ]
    await update.message.reply_text(
        f"✅ Categoria <b>{esc(cat_oficial)}</b> pronta!\n\nEssa categoria vai ter subcategorias?",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )
    return ST_ASK_SUB_CHOICE

async def wiz_decidir_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ST_ASK_SUB_CHOICE

    await query.answer()
    if query.data == "wiz_sub_sim":
        await query.edit_message_text("✏️ Digite o <b>nome da nova subcategoria</b>:", parse_mode="HTML")
        return ST_NEW_SUB

    await query.edit_message_text(
        f"📂 <b>Categoria:</b> {esc(context.user_data['cat_nome'])}\n\n"
        f"<b>Passo 3/5</b> — Selecione um nome ou clique em <b>Criar novo nome</b>:",
        reply_markup=montar_teclado_nomes(context),
        parse_mode="HTML"
    )
    return ST_NOME

async def wiz_escolher_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ST_SUB

    await query.answer()
    if query.data == "wiz_new_sub":
        await query.edit_message_text("✏️ Digite o <b>nome da nova subcategoria</b>:", parse_mode="HTML")
        return ST_NEW_SUB

    sub_id = int(query.data.split("|")[1])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM subcategorias WHERE id = %s", (sub_id,))
    sub_nome = cur.fetchone()[0]
    cur.close()
    conn.close()

    context.user_data["sub_id"] = sub_id
    context.user_data["sub_nome"] = sub_nome

    await query.edit_message_text(
        f"📂 <b>{esc(context.user_data['cat_nome'])} • {esc(sub_nome)}</b>\n\n"
        f"<b>Passo 3/5</b> — Selecione um artista/nome ou clique em <b>Criar novo nome</b>:",
        reply_markup=montar_teclado_nomes(context),
        parse_mode="HTML"
    )
    return ST_NOME

async def wiz_receber_nova_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ST_NEW_SUB

    sub_nome = update.message.text.strip()
    cat_id = context.user_data["cat_id"]

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, nome FROM subcategorias WHERE categoria_id = %s AND LOWER(nome) = LOWER(%s)",
        (cat_id, sub_nome)
    )
    existente = cur.fetchone()
    if existente:
        sub_id, sub_oficial = existente
    else:
        cur.execute(
            "INSERT INTO subcategorias (categoria_id, nome) VALUES (%s, %s) RETURNING id",
            (cat_id, sub_nome)
        )
        sub_id = cur.fetchone()[0]
        sub_oficial = sub_nome
        conn.commit()
    cur.close()
    conn.close()

    context.user_data["sub_id"] = sub_id
    context.user_data["sub_nome"] = sub_oficial

    await update.message.reply_text(
        f"✅ Subcategoria <b>{esc(sub_oficial)}</b> criada!\n\n"
        f"<b>Passo 3/5</b> — Selecione um nome ou clique em <b>Criar novo nome</b>:",
        reply_markup=montar_teclado_nomes(context),
        parse_mode="HTML"
    )
    return ST_NOME

async def wiz_escolher_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ST_NOME

    if query.data == "wiz_new_nome":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>nome do novo artista / personagem</b>:", parse_mode="HTML")
        return ST_NEW_NOME

    idx = int(query.data.split("|")[1])
    nomes_lista = context.user_data.get("nomes_lista", [])
    if idx >= len(nomes_lista):
        await query.answer("Lista expirada, tente novamente.", show_alert=True)
        return ST_NOME

    nome_escolhido = nomes_lista[idx]

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cartas WHERE LOWER(nome) = LOWER(%s)", (nome_escolhido,))
    total_tiers = cur.fetchone()[0]
    cur.close()
    conn.close()

    if total_tiers >= 5:
        await query.answer(f"{nome_escolhido} já está completo (5/5 tiers)!", show_alert=True)
        return ST_NOME

    await query.answer()
    context.user_data["nome_carta"] = nome_escolhido
    await query.edit_message_text(
        f"🎧 <b>Nome selecionado:</b> {esc(nome_escolhido)}\n\n"
        f"<b>Passo 4/5</b> — Envie agora a <b>foto</b> (ou GIF) da carta:\n"
        f"<i>(Fotos são padronizadas automaticamente em {LARGURA_CARTA}×{ALTURA_CARTA})</i>",
        parse_mode="HTML"
    )
    return ST_MIDIA

async def wiz_receber_novo_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ST_NEW_NOME

    novo_nome = update.message.text.strip()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM cartas WHERE LOWER(nome) = LOWER(%s) LIMIT 1", (novo_nome,))
    ja_existe = cur.fetchone()
    cur.close()
    conn.close()

    if ja_existe:
        await update.message.reply_text(
            f"⚠️ <b>{esc(ja_existe[0])}</b> já está cadastrado!\n"
            f"Não é possível criar duplicado — selecione ele na lista ou digite um nome diferente:",
            reply_markup=montar_teclado_nomes(context),
            parse_mode="HTML"
        )
        return ST_NOME

    context.user_data["nome_carta"] = novo_nome
    await update.message.reply_text(
        f"🎧 <b>Novo nome:</b> {esc(novo_nome)}\n\n"
        f"<b>Passo 4/5</b> — Envie agora a <b>foto</b> (ou GIF) da carta:\n"
        f"<i>(Fotos são padronizadas automaticamente em {LARGURA_CARTA}×{ALTURA_CARTA})</i>",
        parse_mode="HTML"
    )
    return ST_MIDIA

async def wiz_receber_midia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ST_MIDIA

    msg = update.message
    if msg.animation:
        context.user_data["raw_file_id"] = msg.animation.file_id
        context.user_data["media_type"] = "animation"
    elif msg.photo:
        context.user_data["raw_file_id"] = msg.photo[-1].file_id
        context.user_data["media_type"] = "photo"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        context.user_data["raw_file_id"] = msg.document.file_id
        context.user_data["media_type"] = "photo"
    else:
        await msg.reply_text("⚠️ Envie uma <b>foto</b> ou <b>GIF</b> válido.", parse_mode="HTML")
        return ST_MIDIA

    nome_carta = context.user_data["nome_carta"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT tier FROM cartas WHERE LOWER(nome) = LOWER(%s)", (nome_carta,))
    ocupados = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.close()

    botoes = []
    for t in range(1, 6):
        t_cfg = TIERS_CONFIG[t]
        if t in ocupados:
            botoes.append([InlineKeyboardButton(f"⛔ {t_cfg['nome']} (Já existe)", callback_data=f"wiz_tier_lock|{t}")])
        else:
            botoes.append([InlineKeyboardButton(f"{t_cfg['emoji']} {t_cfg['nome']}", callback_data=f"wiz_tier|{t}")])
    botoes.append([InlineKeyboardButton("❌ Cancelar", callback_data="wiz_cancel")])

    await msg.reply_text(
        f"<b>Passo 5/5</b> — Escolha o <b>Tier</b> para <b>{esc(nome_carta)}</b>:",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )
    return ST_TIER

async def wiz_escolher_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ST_TIER

    if query.data.startswith("wiz_tier_lock"):
        await query.answer("Esse Tier já está cadastrado para esse nome! Escolha um tier livre.", show_alert=True)
        return ST_TIER

    tier = int(query.data.split("|")[1])
    nome_carta = context.user_data["nome_carta"]
    cat_id = context.user_data["cat_id"]
    cat_nome = context.user_data["cat_nome"]
    sub_id = context.user_data.get("sub_id")
    sub_nome = context.user_data.get("sub_nome")
    raw_file_id = context.user_data["raw_file_id"]
    media_type = context.user_data["media_type"]

    await query.answer("Gerando carta...")
    await query.edit_message_text("⏳ Padronizando carta e finalizando cadastro...")

    if media_type == "photo":
        tg_file = await context.bot.get_file(raw_file_id)
        buf_in = io.BytesIO()
        await tg_file.download_to_memory(buf_in)
        buf_out = processar_imagem_carta(buf_in.getvalue(), tier)

        sent_msg = await query.message.reply_photo(
            photo=buf_out,
            caption="🖼️ <i>Prévia final (1080×1700)</i>",
            parse_mode="HTML"
        )
        final_file_id = sent_msg.photo[-1].file_id
    else:
        final_file_id = raw_file_id

    peso = TIERS_CONFIG[tier]["peso"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO cartas (nome, categoria_id, subcategoria_id, tier, peso_drop, file_id, media_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (nome_carta, cat_id, sub_id, tier, peso, final_file_id, media_type))
    nova_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    t_cfg = TIERS_CONFIG[tier]
    cat_codigo = sub_id if sub_id else cat_id
    local_str = f"{esc(cat_nome)} • {esc(sub_nome)}" if sub_nome else esc(cat_nome)
    await query.message.reply_text(
        f"✅ <b>Carta cadastrada com sucesso!</b>\n\n"
        f"{t_cfg['emoji']} <b>{nova_id}. {esc(nome_carta)}.</b> <i>[{t_cfg['curto']}]</i>\n"
        f"💿 <b>{cat_codigo}. {local_str}</b>",
        parse_mode="HTML"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer("Cancelado.")
        await update.callback_query.edit_message_text("❌ Cadastro de carta cancelado.")
    else:
        await update.message.reply_text("❌ Cadastro de carta cancelado.")
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# COMANDOS EXCLUSIVOS DE ADMIN (/studio)
# ==========================================
async def bloqueio_nao_admin(update: Update) -> bool:
    return not eh_admin(update.effective_user.id)

async def cmd_studio_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return
    texto = (
        "🎛️ <b>Ecco-Mix Studio (Painel Admin)</b>\n\n"
        "<b>Criar & Gerenciar Cartas:</b>\n"
        "• /addcarta — Assistente interativo com botões (Categoria → Subcategoria → Nome → Foto 1080×1700 → Tier)\n"
        "• /editcarta <code>ID | Nome | Categoria | Subcategoria | Tier</code> — Editar carta (use <code>-</code> sem subcategoria)\n"
        "• /delcarta <code>ID</code> — Excluir uma carta\n\n"
        "<b>Molduras PNG por Tier (Opcional):</b>\n"
        "• /moldura <code>1 a 5</code> — Responda a um arquivo <b>.PNG transparente</b> para definir a moldura daquele Tier\n"
        "• /molduras — Ver quais Tiers têm moldura ativa\n"
        "• /remmoldura <code>1 a 5</code> — Remover moldura de um Tier\n\n"
        "<b>Categorias & Subcategorias:</b>\n"
        "• /listacat — Ver IDs e nomes de categorias e subcategorias\n"
        "• /editcat <code>ID | NovoNome</code> — Renomear categoria\n"
        "• /editsub <code>ID | NovoNome</code> — Renomear subcategoria\n\n"
        "<b>Giros & Equipe:</b>\n"
        "• /dargiros <code>qtd</code> (respondendo a alguém) ou <code>ID qtd</code>\n"
        "• /addadm <code>ID</code> (ou responda a alguém) — Adicionar admin\n"
        "• /remadm <code>ID</code> — Remover admin"
    )
    await update.message.reply_text(texto, parse_mode="HTML")

async def cmd_set_moldura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    try:
        tier = int(context.args[0])
        if tier not in TIERS_CONFIG:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: responda a um arquivo <b>.PNG</b> (sem comprimir) com <code>/moldura 1</code> a <code>5</code>.", parse_mode="HTML")
        return

    reply = update.message.reply_to_message
    if not reply or not reply.document or not reply.document.mime_type or "png" not in reply.document.mime_type.lower():
        await update.message.reply_text(
            f"⚠️ Para manter o fundo transparente, envie a moldura como <b>Arquivo (.PNG)</b> (não como foto comprimida) e responda a ela com <code>/moldura {tier}</code>.",
            parse_mode="HTML"
        )
        return

    tg_file = await context.bot.get_file(reply.document.file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    png_bytes = buf.getvalue()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO molduras (tier, png_bytes) VALUES (%s, %s)
        ON CONFLICT (tier) DO UPDATE SET png_bytes = EXCLUDED.png_bytes
    """, (tier, psycopg2.Binary(png_bytes)))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        f"✅ Moldura PNG salva para <b>{TIERS_CONFIG[tier]['nome']}</b>!\n"
        f"Todas as novas fotos enviadas nesse Tier receberão essa moldura em {LARGURA_CARTA}×{ALTURA_CARTA}.",
        parse_mode="HTML"
    )

async def cmd_rem_moldura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    try:
        tier = int(context.args[0])
        if tier not in TIERS_CONFIG:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: <code>/remmoldura 1</code> a <code>5</code>", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM molduras WHERE tier = %s", (tier,))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(f"🗑️ Moldura removida de <b>{TIERS_CONFIG[tier]['nome']}</b>.", parse_mode="HTML")

async def cmd_status_molduras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT tier FROM molduras")
    ativas = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.close()

    linhas = ["🖼️ <b>Status das Molduras PNG</b>\n"]
    for t in range(1, 6):
        status = "✅ Moldura PNG ativa" if t in ativas else "➖ Sem moldura (apenas 1080×1700)"
        linhas.append(f"• <b>{TIERS_CONFIG[t]['nome']}:</b> {status}")

    await update.message.reply_text("\n".join(linhas), parse_mode="HTML")

async def cmd_lista_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM categorias ORDER BY id ASC")
    cats = cur.fetchall()
    cur.execute("SELECT id, categoria_id, nome FROM subcategorias ORDER BY id ASC")
    subs = cur.fetchall()
    cur.close()
    conn.close()

    mapa_subs = {}
    for sid, cid, snome in subs:
        mapa_subs.setdefault(cid, []).append(f"<code>[{sid}]</code> {esc(snome)}")

    linhas = ["📂 <b>Categorias e Subcategorias</b>\n"]
    for cid, cnome in cats:
        linhas.append(f"• <code>[{cid}]</code> <b>{esc(cnome)}</b>")
        if cid in mapa_subs:
            linhas.append(f"   ↳ {', '.join(mapa_subs[cid])}")

    await update.message.reply_text("\n".join(linhas), parse_mode="HTML")

async def cmd_edit_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    partes = [p.strip() for p in " ".join(context.args).split("|")]
    if len(partes) != 2 or not partes[0].isdigit():
        await update.message.reply_text("Uso: <code>/editcat ID | Novo Nome</code>", parse_mode="HTML")
        return

    cid, novo_nome = int(partes[0]), partes[1]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE categorias SET nome = %s WHERE id = %s", (novo_nome, cid))
    alterados = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if alterados:
        await update.message.reply_text(f"✅ Categoria <code>{cid}</code> renomeada para <b>{esc(novo_nome)}</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text("ID de categoria não encontrado.")

async def cmd_edit_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    partes = [p.strip() for p in " ".join(context.args).split("|")]
    if len(partes) != 2 or not partes[0].isdigit():
        await update.message.reply_text("Uso: <code>/editsub ID | Novo Nome</code>", parse_mode="HTML")
        return

    sid, novo_nome = int(partes[0]), partes[1]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE subcategorias SET nome = %s WHERE id = %s", (novo_nome, sid))
    alterados = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if alterados:
        await update.message.reply_text(f"✅ Subcategoria <code>{sid}</code> renomeada para <b>{esc(novo_nome)}</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text("ID de subcategoria não encontrado.")

async def cmd_edit_carta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    partes = [p.strip() for p in " ".join(context.args).split("|")]
    if len(partes) != 5 or not partes[0].replace("#", "").isdigit():
        await update.message.reply_text(
            "Uso: <code>/editcarta ID | NovoNome | Categoria | Subcategoria | Tier</code>\n"
            "(Use <code>-</code> na Subcategoria se não houver.)",
            parse_mode="HTML"
        )
        return

    cid = int(partes[0].replace("#", ""))
    novo_nome, cat_nome, sub_nome, tier_str = partes[1], partes[2], partes[3], partes[4]

    if not tier_str.isdigit() or int(tier_str) not in TIERS_CONFIG:
        await update.message.reply_text("O Tier deve ser de <b>1 a 5</b>.", parse_mode="HTML")
        return
    tier = int(tier_str)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT file_id, media_type FROM cartas WHERE id = %s", (cid,))
    atual = cur.fetchone()
    if not atual:
        await update.message.reply_text("Carta não encontrada.")
        cur.close()
        conn.close()
        return

    cur.execute("SELECT id FROM categorias WHERE LOWER(nome) = LOWER(%s)", (cat_nome,))
    cat = cur.fetchone()
    if not cat:
        await update.message.reply_text("Categoria não encontrada.")
        cur.close()
        conn.close()
        return
    cat_id = cat[0]

    sub_id = None
    if sub_nome != "-":
        cur.execute("SELECT id FROM subcategorias WHERE categoria_id = %s AND LOWER(nome) = LOWER(%s)", (cat_id, sub_nome))
        sub = cur.fetchone()
        if not sub:
            await update.message.reply_text("Subcategoria não encontrada nessa categoria.")
            cur.close()
            conn.close()
            return
        sub_id = sub[0]

    cur.execute("SELECT id FROM cartas WHERE LOWER(nome) = LOWER(%s) AND tier = %s AND id != %s", (novo_nome, tier, cid))
    conflito = cur.fetchone()
    if conflito:
        await update.message.reply_text(f"⚠️ Já existe outra carta de <b>{esc(novo_nome)}</b> no Tier {tier} (<code>#{conflito[0]}</code>).", parse_mode="HTML")
        cur.close()
        conn.close()
        return

    file_id, media_type = atual
    reply = update.message.reply_to_message
    if reply:
        if reply.animation:
            file_id = reply.animation.file_id
            media_type = "animation"
        elif reply.photo:
            tg_file = await context.bot.get_file(reply.photo[-1].file_id)
            buf_in = io.BytesIO()
            await tg_file.download_to_memory(buf_in)
            buf_out = processar_imagem_carta(buf_in.getvalue(), tier)
            sent_msg = await update.message.reply_photo(photo=buf_out)
            file_id = sent_msg.photo[-1].file_id
            media_type = "photo"

    peso = TIERS_CONFIG[tier]["peso"]
    cur.execute("""
        UPDATE cartas
        SET nome = %s, categoria_id = %s, subcategoria_id = %s, tier = %s, peso_drop = %s, file_id = %s, media_type = %s
        WHERE id = %s
    """, (novo_nome, cat_id, sub_id, tier, peso, file_id, media_type, cid))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(f"✅ Carta <code>#{cid}</code> atualizada com sucesso!", parse_mode="HTML")

async def cmd_del_carta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    try:
        cid = int(context.args[0].replace("#", ""))
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: <code>/delcarta ID</code>", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM cartas WHERE id = %s", (cid,))
    apagou = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if apagou:
        await update.message.reply_text(f"🗑️ Carta <code>#{cid}</code> excluída.", parse_mode="HTML")
    else:
        await update.message.reply_text("Carta não encontrada.")

async def cmd_dar_giros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    try:
        if update.message.reply_to_message:
            alvo = update.message.reply_to_message.from_user
            alvo_id = alvo.id
            qtd = int(context.args[0])
            verificar_usuario(alvo_id, alvo.username)
        else:
            alvo_id = int(context.args[0])
            qtd = int(context.args[1])
            verificar_usuario(alvo_id)
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: responda à pessoa com <code>/dargiros 5</code> ou <code>/dargiros ID 5</code>", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE usuarios SET giros_restantes = giros_restantes + %s WHERE user_id = %s", (qtd, alvo_id))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(f"🍃 Adicionados <b>{qtd}</b> giros para <code>{alvo_id}</code>.", parse_mode="HTML")

async def cmd_add_adm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in SUPER_ADMINS:
        return

    try:
        if update.message.reply_to_message:
            novo_adm = update.message.reply_to_message.from_user.id
        else:
            novo_adm = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Responda à pessoa com <code>/addadm</code> ou use <code>/addadm ID</code>.", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (novo_adm,))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"🛡️ Usuário <code>{novo_adm}</code> agora é administrador.", parse_mode="HTML")

async def cmd_rem_adm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in SUPER_ADMINS:
        return

    try:
        alvo_adm = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: <code>/remadm ID</code>", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE user_id = %s", (alvo_adm,))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"✅ Permissões de admin removidas de <code>{alvo_adm}</code>.", parse_mode="HTML")

# ==========================================
# INICIALIZAÇÃO DO SERVIDOR (RENDER + NEON)
# ==========================================
async def main():
    iniciar_banco()
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    # Assistente interativo /addcarta
    conv_carta = ConversationHandler(
        entry_points=[CommandHandler("addcarta", wiz_start)],
        states={
            ST_CAT: [CallbackQueryHandler(wiz_escolher_cat, pattern=r"^wiz_(cat\|.+|new_cat)$")],
            ST_NEW_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_receber_nova_cat)],
            ST_ASK_SUB_CHOICE: [CallbackQueryHandler(wiz_decidir_sub, pattern=r"^wiz_sub_(sim|nao)$")],
            ST_SUB: [CallbackQueryHandler(wiz_escolher_sub, pattern=r"^wiz_(sub\|.+|new_sub)$")],
            ST_NEW_SUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_receber_nova_sub)],
            ST_NOME: [CallbackQueryHandler(wiz_escolher_nome, pattern=r"^wiz_(nome\|.+|new_nome)$")],
            ST_NEW_NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_receber_novo_nome)],
            ST_MIDIA: [MessageHandler(filters.PHOTO | filters.Document.IMAGE | filters.ANIMATION, wiz_receber_midia)],
            ST_TIER: [CallbackQueryHandler(wiz_escolher_tier, pattern=r"^wiz_tier")],
        },
        fallbacks=[
            CommandHandler("cancelar", wiz_cancel),
            CallbackQueryHandler(wiz_cancel, pattern=r"^wiz_cancel$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv_carta)

    # Comandos de Jogadores
    app.add_handler(CommandHandler(["start", "ajuda"], cmd_start))
    app.add_handler(CommandHandler("apelido", cmd_apelido))
    app.add_handler(CommandHandler("meuid", cmd_meu_id))
    app.add_handler(CommandHandler("girar", cmd_girar))
    app.add_handler(CommandHandler("artista", cmd_artista))
    app.add_handler(CommandHandler("colecao", cmd_colecao))
    app.add_handler(CommandHandler("carta", cmd_carta))
    app.add_handler(CommandHandler("trocar", cmd_trocar))

    # Comandos de Admin (Studio)
    app.add_handler(CommandHandler("studio", cmd_studio_help))
    app.add_handler(CommandHandler("moldura", cmd_set_moldura))
    app.add_handler(CommandHandler("remmoldura", cmd_rem_moldura))
    app.add_handler(CommandHandler("molduras", cmd_status_molduras))
    app.add_handler(CommandHandler("listacat", cmd_lista_cat))
    app.add_handler(CommandHandler("editcat", cmd_edit_cat))
    app.add_handler(CommandHandler("editsub", cmd_edit_sub))
    app.add_handler(CommandHandler("editcarta", cmd_edit_carta))
    app.add_handler(CommandHandler("delcarta", cmd_del_carta))
    app.add_handler(CommandHandler("dargiros", cmd_dar_giros))
    app.add_handler(CommandHandler("addadm", cmd_add_adm))
    app.add_handler(CommandHandler("remadm", cmd_rem_adm))

    # Botões Interativos do Jogo
    app.add_handler(CallbackQueryHandler(callback_girar_categoria, pattern=r"^rcat\|"))
    app.add_handler(CallbackQueryHandler(callback_girar_subcategoria, pattern=r"^rsub\|"))
    app.add_handler(CallbackQueryHandler(callback_troca, pattern=r"^trade\|"))

    async def handle_webhook(request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.update_queue.put(update)
        return web.Response(text="OK")

    async def handle_health(request):
        return web.Response(text="Ecco-Mix Trick online!")

    web_app = web.Application()
    web_app.router.add_post("/webhook", handle_webhook)
    web_app.router.add_get("/", handle_health)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await app.initialize()
    await app.start()
    await app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook", allowed_updates=Update.ALL_TYPES)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
