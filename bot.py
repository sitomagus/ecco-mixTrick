import os
import html
import random
import asyncio
import psycopg2
from datetime import date
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# CONFIGURAÇÕES DO ECCO-MIX TRICK
# ==========================================
TOKEN = os.environ["BOT_TOKEN"].strip()
DATABASE_URL = os.environ["DATABASE_URL"].strip()
WEBHOOK_URL = os.environ["WEBHOOK_URL"].strip().rstrip("/")
PORT = int(os.environ.get("PORT", 8080))

SUPER_ADMINS = [123456789]
env_admins = os.environ.get("ADMIN_IDS", "")
if env_admins:
    SUPER_ADMINS.extend([int(x.strip()) for x in env_admins.split(",") if x.strip().isdigit()])

GIROS_DIARIOS = 10

TIERS_CONFIG = {
    1: {"nome": "Tier 1 (Comum)", "peso": 48},
    2: {"nome": "Tier 2 (Incomum)", "peso": 27},
    3: {"nome": "Tier 3 (Raro)", "peso": 15},
    4: {"nome": "Tier 4 (Épico)", "peso": 7},
    5: {"nome": "Tier 5 (Lendário)", "peso": 3},
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
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            nome TEXT,
            giros_restantes INTEGER DEFAULT 10,
            ultimo_reset TEXT
        );
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

def verificar_usuario(user_id: int, username: str = "", nome: str = "Jogador") -> int:
    conn = get_db()
    cur = conn.cursor()
    hoje = str(date.today())
    username_limpo = (username or "").lstrip("@").lower()

    cur.execute("SELECT giros_restantes, ultimo_reset FROM usuarios WHERE user_id = %s", (user_id,))
    row = cur.fetchone()

    if not row:
        cur.execute(
            "INSERT INTO usuarios (user_id, username, nome, giros_restantes, ultimo_reset) VALUES (%s, %s, %s, %s, %s)",
            (user_id, username_limpo, nome, GIROS_DIARIOS, hoje)
        )
        giros = GIROS_DIARIOS
    else:
        giros, ultimo_reset = row
        if username_limpo:
            cur.execute("UPDATE usuarios SET username = %s, nome = %s WHERE user_id = %s", (username_limpo, nome, user_id))
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
    giros = verificar_usuario(user.id, user.username, user.first_name)
    texto = (
        f"🎛️ <b>Ecco-Mix Trick</b>\n\n"
        f"Você tem <b>{giros}</b> giros disponíveis hoje.\n\n"
        f"<b>Comandos disponíveis:</b>\n"
        f"• /girar — Escolher uma categoria e sortear uma carta\n"
        f"• /colecao — Ver toda a sua coleção agrupada\n"
        f"• /artista <code>nome</code> — Ver seu progresso nos 5 tiers de um artista/personagem\n"
        f"• /carta <code>ID</code> — Visualizar a imagem/GIF de uma carta\n"
        f"• /trocar <code>minha_id</code> <code>id_dele</code> — Propor troca (responda à mensagem da pessoa)\n"
        f"• /meuid — Ver seu ID numérico do Telegram"
    )
    if eh_admin(user.id):
        texto += "\n\n🛡️ <i>Você é Admin. Use /admin para ver o painel de gerenciamento.</i>"

    await update.message.reply_text(texto, parse_mode="HTML")

async def cmd_meu_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Seu ID numérico é: <code>{update.effective_user.id}</code>", parse_mode="HTML")

async def cmd_girar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    giros = verificar_usuario(user.id, user.username, user.first_name)

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

    botoes.append([InlineKeyboardButton("🎲 Mix Aleatório (Tudo)", callback_data=f"rcat|all|{user.id}")])

    await update.message.reply_text(
        f"🎟️ Giros restantes: <b>{giros}</b>\nEscolha a categoria:",
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

    botoes.append([InlineKeyboardButton(f"🎲 Todos de {cat_row[0]}", callback_data=f"rsub|{cat_id}|all|{dono_id}")])

    await query.answer()
    await query.edit_message_text(
        f"🎧 <b>{html.escape(cat_row[0])}</b> — Escolha a categoria:",
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
    giros = verificar_usuario(user.id, user.username, user.first_name)
    if giros <= 0:
        await query.answer("Seus giros acabaram!", show_alert=True)
        return

    conn = get_db()
    cur = conn.cursor()

    sql_base = """
        SELECT c.id, c.nome, cat.nome, sub.nome, c.tier, c.peso_drop, c.file_id, c.media_type
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

    pesos = [c[5] for c in cartas]
    carta_id, nome_carta, nome_cat, nome_sub, tier, _, file_id, media_type = random.choices(cartas, weights=pesos, k=1)[0]

    cur.execute("UPDATE usuarios SET giros_restantes = giros_restantes - 1 WHERE user_id = %s", (user.id,))
    cur.execute("SELECT id, quantidade FROM inventario WHERE user_id = %s AND carta_id = %s", (user.id, carta_id))
    item = cur.fetchone()

    if item:
        cur.execute("UPDATE inventario SET quantidade = quantidade + 1 WHERE id = %s", (item[0],))
        status_carta = f"Repetida ({item[1] + 1}x)"
    else:
        cur.execute("INSERT INTO inventario (user_id, carta_id, quantidade) VALUES (%s, %s, 1)", (user.id, carta_id))
        status_carta = "Nova na coleção!"

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

    local_str = f"{html.escape(nome_cat)} • {html.escape(nome_sub)}" if nome_sub else html.escape(nome_cat)
    tier_label = TIERS_CONFIG[tier]["nome"]

    legenda = (
        f"✨ <b>Ecco-Mix Drop!</b> (<code>#{carta_id}</code>)\n\n"
        f"🃏 <b>Nome:</b> {html.escape(nome_carta)} ({tiers_obtidos}/{tiers_cadastrados} tiers)\n"
        f"📂 <b>Categoria:</b> {local_str}\n"
        f"💎 <b>Raridade:</b> {tier_label}\n"
        f"📦 <b>Status:</b> {status_carta}\n"
        f"🎟️ Giros restantes: <b>{giros - 1}</b>"
    )
    await enviar_midia_carta(query.message, file_id, media_type, legenda)

async def cmd_artista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    verificar_usuario(user.id, user.username, user.first_name)

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

    local_str = f"{html.escape(cat_nome)} • {html.escape(sub_nome)}" if sub_nome else html.escape(cat_nome)
    obtidas = len(posse)
    total = len(mapa_tiers)

    linhas = [
        f"🎤 <b>{html.escape(nome_oficial)}</b> <i>({local_str})</i>",
        f"📊 <b>Progresso:</b> {obtidas}/{total} cartas\n"
    ]

    for t in range(1, 6):
        label_t = TIERS_CONFIG[t]["nome"]
        if t in mapa_tiers:
            cid = mapa_tiers[t]
            if t in posse:
                linhas.append(f"• <b>{label_t}:</b> ✅ (<code>#{cid}</code> — {posse[t]}x)")
            else:
                linhas.append(f"• <b>{label_t}:</b> ❌ (<code>#{cid}</code>)")
        else:
            linhas.append(f"• <b>{label_t}:</b> ➖ <i>(Não cadastrada)</i>")

    await update.message.reply_text("\n".join(linhas), parse_mode="HTML")

async def cmd_colecao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    verificar_usuario(user.id, user.username, user.first_name)

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

    linhas = [f"🗂️ <b>Coleção de {html.escape(user.first_name)}</b>\n"]
    for nome, lista in agrupado.items():
        total_existentes = totais.get(nome.lower(), len(lista))
        selo = "⭐ " if len(lista) == 5 else ""
        resumo_tiers = ", ".join([f"T{t} (<code>#{cid}</code> x{qtd})" for cid, t, qtd in lista])
        linhas.append(f"{selo}<b>{html.escape(nome)}</b> [{len(lista)}/{total_existentes}]: {resumo_tiers}")

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
        SELECT c.id, c.nome, cat.nome, sub.nome, c.tier, c.file_id, c.media_type
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

    cid, nome, nome_cat, nome_sub, tier, file_id, media_type = carta
    local_str = f"{html.escape(nome_cat)} • {html.escape(nome_sub)}" if nome_sub else html.escape(nome_cat)
    legenda = (
        f"🃏 <b>Carta #{cid}</b>\n"
        f"🎤 <b>Nome:</b> {html.escape(nome)}\n"
        f"📂 <b>Categoria:</b> {local_str}\n"
        f"💎 <b>Raridade:</b> {TIERS_CONFIG[tier]['nome']}"
    )
    await enviar_midia_carta(update.message, file_id, media_type, legenda)

# ==========================================
# SISTEMA DE TROCAS (JOGADORES)
# ==========================================
async def cmd_trocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remetente = update.effective_user
    verificar_usuario(remetente.id, remetente.username, remetente.first_name)

    destinatario_id = None
    destinatario_nome = None

    if update.message.reply_to_message and len(context.args) >= 2:
        alvo = update.message.reply_to_message.from_user
        if alvo.is_bot or alvo.id == remetente.id:
            await update.message.reply_text("Responda à mensagem de outro jogador válido.")
            return
        destinatario_id = alvo.id
        destinatario_nome = alvo.first_name
        verificar_usuario(destinatario_id, alvo.username, alvo.first_name)
        id_str_1, id_str_2 = context.args[0], context.args[1]
    elif len(context.args) >= 3 and context.args[0].startswith("@"):
        uname = context.args[0].lstrip("@").lower()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT user_id, nome FROM usuarios WHERE username = %s", (uname,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            await update.message.reply_text("Jogador não encontrado. Peça para ele usar /girar primeiro ou responda uma mensagem dele.")
            return
        destinatario_id, destinatario_nome = row
        if destinatario_id == remetente.id:
            await update.message.reply_text("Você não pode trocar cartas consigo mesmo.")
            return
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
        await update.message.reply_text(f"{html.escape(destinatario_nome)} não possui a carta <code>#{c_pedida}</code>.", parse_mode="HTML")
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
        f"👤 <b>{html.escape(remetente.first_name)}</b> oferece:\n"
        f"   🃏 <code>#{c_oferecida}</code> — {html.escape(info1[0])} (Tier {info1[1]})\n\n"
        f"👤 Para <b>{html.escape(destinatario_nome)}</b> em troca de:\n"
        f"   🃏 <code>#{c_pedida}</code> — {html.escape(info2[0])} (Tier {info2[1]})"
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
# COMANDOS EXCLUSIVOS DE ADMIN
# ==========================================
async def bloqueio_nao_admin(update: Update) -> bool:
    if not eh_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Comando exclusivo para administradores.")
        return True
    return False

async def cmd_admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return
    texto = (
        "🛡️ <b>Painel Admin — Ecco-Mix Trick</b>\n\n"
        "<b>Categorias & Subcategorias:</b>\n"
        "• /listacat — Ver IDs e nomes de todas categorias e subcategorias\n"
        "• /addcat <code>Nome</code> — Criar categoria\n"
        "• /editcat <code>ID | NovoNome</code> — Editar nome da categoria\n"
        "• /addsub <code>Categoria | NomeSub</code> — Criar subcategoria\n"
        "• /editsub <code>ID | NovoNome</code> — Editar nome da subcategoria\n\n"
        "<b>Cartas (1 por Tier de 1 a 5):</b>\n"
        "• /addcarta <code>Categoria | Subcategoria | Nome | Tier</code> (responda à foto/GIF; se não tiver subcategoria, use <code>Categoria | Nome | Tier</code>)\n"
        "• /editcarta <code>ID | Nome | Categoria | Subcategoria | Tier</code> (use <code>-</code> se não houver subcategoria; pode responder a uma nova foto/GIF para trocar a mídia)\n"
        "• /delcarta <code>ID</code> — Excluir uma carta\n\n"
        "<b>Giros & Permissões:</b>\n"
        "• /dargiros <code>qtd</code> (respondendo a alguém) ou <code>ID qtd</code>\n"
        "• /addadm <code>ID</code> (ou responda a alguém) — Dar poder de admin\n"
        "• /remadm <code>ID</code> — Remover admin"
    )
    await update.message.reply_text(texto, parse_mode="HTML")

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
        mapa_subs.setdefault(cid, []).append(f"<code>[{sid}]</code> {html.escape(snome)}")

    linhas = ["📂 <b>Categorias e Subcategorias</b>\n"]
    for cid, cnome in cats:
        linhas.append(f"• <code>[{cid}]</code> <b>{html.escape(cnome)}</b>")
        if cid in mapa_subs:
            linhas.append(f"   ↳ {', '.join(mapa_subs[cid])}")

    await update.message.reply_text("\n".join(linhas), parse_mode="HTML")

async def cmd_add_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    nome = " ".join(context.args).strip()
    if not nome:
        await update.message.reply_text("Uso: <code>/addcat Nome da Categoria</code>", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO categorias (nome) VALUES (%s) RETURNING id", (nome,))
        cid = cur.fetchone()[0]
        conn.commit()
        await update.message.reply_text(f"✅ Categoria <b>{html.escape(nome)}</b> criada (ID <code>{cid}</code>).", parse_mode="HTML")
    except psycopg2.IntegrityError:
        conn.rollback()
        await update.message.reply_text("Já existe uma categoria com esse nome.")
    finally:
        cur.close()
        conn.close()

async def cmd_edit_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    partes = [p.strip() for p in " ".join(context.args).split("|")]
    if len(partes) != 2 or not partes[0].isdigit():
        await update.message.reply_text("Uso: <code>/editcat ID | Novo Nome</code> (veja o ID em /listacat)", parse_mode="HTML")
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
        await update.message.reply_text(f"✅ Categoria <code>{cid}</code> renomeada para <b>{html.escape(novo_nome)}</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text("ID de categoria não encontrado.")

async def cmd_add_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    partes = [p.strip() for p in " ".join(context.args).split("|")]
    if len(partes) != 2:
        await update.message.reply_text("Uso: <code>/addsub Categoria | Nome da Subcategoria</code>", parse_mode="HTML")
        return

    cat_nome, sub_nome = partes
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM categorias WHERE LOWER(nome) = LOWER(%s)", (cat_nome,))
    cat = cur.fetchone()
    if not cat:
        await update.message.reply_text("Categoria não encontrada. Use /listacat para conferir.")
        cur.close()
        conn.close()
        return

    try:
        cur.execute("INSERT INTO subcategorias (categoria_id, nome) VALUES (%s, %s) RETURNING id", (cat[0], sub_nome))
        sid = cur.fetchone()[0]
        conn.commit()
        await update.message.reply_text(
            f"✅ Subcategoria <b>{html.escape(sub_nome)}</b> (ID <code>{sid}</code>) adicionada em <b>{html.escape(cat[1])}</b>.",
            parse_mode="HTML"
        )
    except psycopg2.IntegrityError:
        conn.rollback()
        await update.message.reply_text("Essa subcategoria já existe nessa categoria.")
    finally:
        cur.close()
        conn.close()

async def cmd_edit_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    partes = [p.strip() for p in " ".join(context.args).split("|")]
    if len(partes) != 2 or not partes[0].isdigit():
        await update.message.reply_text("Uso: <code>/editsub ID | Novo Nome</code> (veja o ID em /listacat)", parse_mode="HTML")
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
        await update.message.reply_text(f"✅ Subcategoria <code>{sid}</code> renomeada para <b>{html.escape(novo_nome)}</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text("ID de subcategoria não encontrado.")

def extrair_midia(reply_msg):
    if not reply_msg:
        return None, None
    if reply_msg.animation:
        return reply_msg.animation.file_id, "animation"
    if reply_msg.photo:
        return reply_msg.photo[-1].file_id, "photo"
    return None, None

async def cmd_add_carta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    file_id, media_type = extrair_midia(update.message.reply_to_message)
    if not file_id:
        await update.message.reply_text(
            "Responda a uma <b>foto ou GIF</b> com:\n"
            "• Com subcategoria: <code>/addcarta Música | Underground Rap | Kuru | 5</code>\n"
            "• Sem subcategoria: <code>/addcarta Animes | Killua | 4</code>",
            parse_mode="HTML"
        )
        return

    partes = [p.strip() for p in " ".join(context.args).split("|")]
    if len(partes) == 4:
        cat_nome, sub_nome, nome_carta, tier_str = partes
    elif len(partes) == 3:
        cat_nome, nome_carta, tier_str = partes
        sub_nome = None
    else:
        await update.message.reply_text("Formato inválido. Separe os campos com <code>|</code>.", parse_mode="HTML")
        return

    if not tier_str.isdigit() or int(tier_str) not in TIERS_CONFIG:
        await update.message.reply_text("O Tier deve ser um número de <b>1 a 5</b>.", parse_mode="HTML")
        return
    tier = int(tier_str)

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id, nome FROM categorias WHERE LOWER(nome) = LOWER(%s)", (cat_nome,))
    cat = cur.fetchone()
    if not cat:
        await update.message.reply_text("Categoria não encontrada. Use /listacat.")
        cur.close()
        conn.close()
        return
    cat_id, cat_oficial = cat

    sub_id, sub_oficial = None, None
    if sub_nome and sub_nome != "-":
        cur.execute("SELECT id, nome FROM subcategorias WHERE categoria_id = %s AND LOWER(nome) = LOWER(%s)", (cat_id, sub_nome))
        sub = cur.fetchone()
        if not sub:
            await update.message.reply_text(f"Subcategoria não encontrada em <b>{html.escape(cat_oficial)}</b>.", parse_mode="HTML")
            cur.close()
            conn.close()
            return
        sub_id, sub_oficial = sub

    cur.execute("SELECT id FROM cartas WHERE LOWER(nome) = LOWER(%s) AND tier = %s", (nome_carta, tier))
    existente = cur.fetchone()
    if existente:
        await update.message.reply_text(
            f"⚠️ <b>{html.escape(nome_carta)}</b> já possui uma carta <b>Tier {tier}</b> (ID <code>#{existente[0]}</code>).\n"
            f"Use <code>/editcarta</code> se quiser alterá-la.",
            parse_mode="HTML"
        )
        cur.close()
        conn.close()
        return

    peso = TIERS_CONFIG[tier]["peso"]
    cur.execute("""
        INSERT INTO cartas (nome, categoria_id, subcategoria_id, tier, peso_drop, file_id, media_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (nome_carta, cat_id, sub_id, tier, peso, file_id, media_type))
    nova_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    local_str = f"{html.escape(cat_oficial)} • {html.escape(sub_oficial)}" if sub_oficial else html.escape(cat_oficial)
    await update.message.reply_text(
        f"✅ <b>Carta cadastrada!</b>\n"
        f"🆔 <b>ID:</b> <code>#{nova_id}</code>\n"
        f"🎤 <b>Nome:</b> {html.escape(nome_carta)}\n"
        f"📂 <b>Categoria:</b> {local_str}\n"
        f"💎 <b>Raridade:</b> {TIERS_CONFIG[tier]['nome']}",
        parse_mode="HTML"
    )

async def cmd_edit_carta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await bloqueio_nao_admin(update):
        return

    partes = [p.strip() for p in " ".join(context.args).split("|")]
    if len(partes) != 5 or not partes[0].replace("#", "").isdigit():
        await update.message.reply_text(
            "Uso: <code>/editcarta ID | NovoNome | Categoria | Subcategoria | Tier</code>\n"
            "(Use <code>-</code> na Subcategoria se não houver. Se responder a uma foto/GIF, a imagem também é atualizada.)",
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
        await update.message.reply_text(f"⚠️ Já existe outra carta de <b>{html.escape(novo_nome)}</b> no Tier {tier} (<code>#{conflito[0]}</code>).", parse_mode="HTML")
        cur.close()
        conn.close()
        return

    novo_file_id, novo_media = extrair_midia(update.message.reply_to_message)
    file_id = novo_file_id or atual[0]
    media_type = novo_media or atual[1]
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
            verificar_usuario(alvo_id, alvo.username, alvo.first_name)
        else:
            alvo_id = int(context.args[0])
            qtd = int(context.args[1])
            verificar_usuario(alvo_id)
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Uso:\n• Responda à mensagem da pessoa: <code>/dargiros 5</code>\n"
            "• Ou informe o ID: <code>/dargiros 123456789 5</code>",
            parse_mode="HTML"
        )
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE usuarios SET giros_restantes = giros_restantes + %s WHERE user_id = %s", (qtd, alvo_id))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(f"🎟️ Adicionados <b>{qtd}</b> giros para <code>{alvo_id}</code>.", parse_mode="HTML")

async def cmd_add_adm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in SUPER_ADMINS:
        await update.message.reply_text("⛔ Apenas os donos principais podem adicionar administradores.")
        return

    try:
        if update.message.reply_to_message:
            novo_adm = update.message.reply_to_message.from_user.id
        else:
            novo_adm = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Responda à mensagem da pessoa com <code>/addadm</code> ou use <code>/addadm ID</code>.", parse_mode="HTML")
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
        await update.message.reply_text("⛔ Apenas os donos principais podem remover administradores.")
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

    # Comandos de Jogadores
    app.add_handler(CommandHandler(["start", "ajuda"], cmd_start))
    app.add_handler(CommandHandler("meuid", cmd_meu_id))
    app.add_handler(CommandHandler("girar", cmd_girar))
    app.add_handler(CommandHandler("artista", cmd_artista))
    app.add_handler(CommandHandler("colecao", cmd_colecao))
    app.add_handler(CommandHandler("carta", cmd_carta))
    app.add_handler(CommandHandler("trocar", cmd_trocar))

    # Comandos de Admin
    app.add_handler(CommandHandler("admin", cmd_admin_help))
    app.add_handler(CommandHandler("listacat", cmd_lista_cat))
    app.add_handler(CommandHandler("addcat", cmd_add_cat))
    app.add_handler(CommandHandler("editcat", cmd_edit_cat))
    app.add_handler(CommandHandler("addsub", cmd_add_sub))
    app.add_handler(CommandHandler("editsub", cmd_edit_sub))
    app.add_handler(CommandHandler("addcarta", cmd_add_carta))
    app.add_handler(CommandHandler("editcarta", cmd_edit_carta))
    app.add_handler(CommandHandler("delcarta", cmd_del_carta))
    app.add_handler(CommandHandler("dargiros", cmd_dar_giros))
    app.add_handler(CommandHandler("addadm", cmd_add_adm))
    app.add_handler(CommandHandler("remadm", cmd_rem_adm))

    # Botões Interativos
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
