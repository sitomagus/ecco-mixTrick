import io
import os
import re
import html
import time
import random
import asyncio
from collections import Counter
import psycopg2
from psycopg2.pool import ThreadedConnectionPool, PoolError
from datetime import date, datetime, time as datetime_time, timedelta
from zoneinfo import ZoneInfo
from aiohttp import web
from PIL import Image, ImageOps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
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

try:
    SUPER_ADMIN_ID = int(os.environ["SUPER_ADMIN_ID"].strip())
except (KeyError, ValueError) as exc:
    raise RuntimeError("Configure SUPER_ADMIN_ID nas variáveis de ambiente do Render.") from exc
SUPER_ADMINS = [SUPER_ADMIN_ID]

GIROS_DIARIOS = 10
FUSO_GIROS = ZoneInfo("America/Sao_Paulo")
LARGURA_CARTA = 900
ALTURA_CARTA = 1200
ED_PAGE_SIZE = 10
CAT_PAGE_SIZE = 15

# Trava em memória para evitar clique duplo simultâneo no giro
GIROS_EM_ANDAMENTO: set[int] = set()

# Pool de conexões e cache em memória para textos/emojis
DB_POOL: ThreadedConnectionPool | None = None
_CONFIG_CACHE: dict[str, str] = {}
_CONFIG_LOADED = False

TIERS_CONFIG = {
    6: {"nome": "SS+ (Ultra raro)", "curto": "SS+", "emoji": "💠", "peso": 1},
    1: {"nome": "Tier 1 (Comum)", "curto": "Tier 1", "emoji": "🌱", "peso": 48},
    2: {"nome": "Tier 2 (Incomum)", "curto": "Tier 2", "emoji": "💧", "peso": 27},
    3: {"nome": "Tier 3 (Raro)", "curto": "Tier 3", "emoji": "🦋", "peso": 15},
    4: {"nome": "Tier 4 (Épico)", "curto": "Tier 4", "emoji": "🔮", "peso": 7},
    5: {"nome": "Tier 5 (Lendário)", "curto": "Tier 5", "emoji": "💠", "peso": 3},
}
CHANCE_SS_PLUS = 0.0001

TEXTOS_PADRAO = {
    "start_texto": (
        "🌐 <b>Ecco-Mix Trick</b>\n"
        "🫧 Jogador: <b>{apelido}</b>\n"
        "🍃 Giros disponíveis hoje: <b>{giros}</b>\n\n"
        "<b>Comandos disponíveis:</b>\n"
        "• /girar — Escolher uma categoria e sortear uma carta (ou <code>/girar tudo</code>)\n"
        "• /apelido <code>nome</code> — Definir seu nome único de exibição no jogo"
        "{dica_apelido}"
    ),
    "comandos_texto": (
        "📜 <b>Comandos do Ecco-Mix Trick</b>\n\n"
        "• /colecao — Ver toda a sua coleção agrupada\n"
        "• /catalogo — Ver todas as cartas cadastradas por categoria\n"
        "• /artista <code>nome</code> — Ver seu progresso nos tiers de um artista/personagem\n"
        "• /ranking <code>nome</code> — Ver ranking de jogadores de um artista/personagem\n"
        "• /carta <code>ID</code> — Visualizar a imagem/GIF de uma carta\n"
        "• /trocar <code>@pessoa</code> ou apelido — Propor troca e escolher cartas pelos botões\n"
        "• /doar <code>@pessoa</code> ou apelido — Doar cartas pelo menu de botões\n"
        "• /wishlist [@pessoa ou apelido] — Ver e editar sua wishlist ou consultar a de alguém\n"
        "• /loja — Vender cartas por EccoCoins e comprar giros\n"
        "• /meuid — Ver seu ID numérico do Telegram"
    ),
    "menu_girar": (
        "🫧 <b>{apelido}</b> • 🍃 Giros restantes: <b>{giros}</b>\n"
        "Escolha a categoria:"
    ),
    "giro_resultado": (
        "🌐 <b>MIX SINTONIZADO! Você desbloqueou:</b>\n\n"
        "{tier_emoji} <b>{carta_id}. {nome}.</b> <i>[{tier_curto}]</i>\n"
        "💿 <b>{cat_codigo}. {local}</b>\n\n"
        "🫧 <b>{apelido}.</b> ({qtd}x) ({tiers_obtidos}/{tiers_cadastrados})\n"
        "🌿 <i>Giros restantes: <b>{giros_restantes}</b></i>"
    ),
    "giro_multi": (
        "🌐 <b>MIX MÚLTIPLO SINTONIZADO ({qtd_giros}x)!</b>\n"
        "🫧 <b>{apelido}</b> • 🌿 <i>Giros restantes: <b>{giros_restantes}</b></i>"
    ),
    "giro_multi_item": "{tier_emoji} <code>#{carta_id}</code> <b>{nome}</b> <i>[{tier_curto}]</i>{vezes_txt}{tag_nova}",
    "giro_rodape": (
        "🎰 <b>Giro realizado por {apelido}!</b>\n"
        "<i>Digite /girar para girar novamente.</i>"
    ),
    "sem_giros": "⏳ Seus giros acabaram por hoje! Volte amanhã.",
    "troca_proposta": (
        "🔄 <b>Proposta de troca #{troca_id}</b>\n\n"
        "🫧 <b>{remetente}</b> oferece <b>{oferecida}</b> (Tier {tier_oferecida})\n"
        "em troca de <b>{pedida}</b> (Tier {tier_pedida})."
    ),
    "troca_enviada": "✅ Proposta enviada para <b>{destinatario}</b>.",
    "troca_aceita": "✅ <b>Troca #{troca_id} concluída!</b>",
    "troca_recusada": "❌ Troca #{troca_id} recusada.",
    "troca_status_privado": "🔔 Atualização da troca #{troca_id}: {status}.",
    "troca_status_grupo": "🔔 {remetente} e {destinatario}: troca #{troca_id} {status}.",
    "doacao_prompt": "Escolha a carta que deseja doar para <b>{destinatario}</b>:",
    "doacao_quantidade": "Quantas cópias de <b>{carta}</b> deseja doar? Você tem {quantidade}.",
    "doacao_recebida": "🎁 Você recebeu {quantidade}x <b>{carta}</b> de <b>{remetente}</b>!",
    "doacao_enviada": "✅ Você doou {quantidade}x <b>{carta}</b> para <b>{destinatario}</b>.",
    "doacao_privado_enviada": "🎁 Você doou {quantidade}x <b>{carta}</b> para <b>{destinatario}</b>.",
    "doacao_grupo": "🎁 {remetente} doou {quantidade}x <b>{carta}</b> para {destinatario}!",
    "loja_menu": (
        "🫧 <b>EccoCoin</b>\n\nSaldo: <b>{saldo}</b> EccoCoins\nGiros: <b>{giros}</b>\n\n"
        "Venda cartas por 1 EccoCoin cada. Cada giro custa 10 EccoCoins."
    ),
    "loja_venda_prompt": "Escolha a carta que quer vender ao bot:",
    "loja_venda_quantidade": "Você tem {quantidade}x <b>{carta}</b>. Cada cópia rende 1 EccoCoin. Quantas vai vender?",
    "loja_busca_prompt": "Digite o nome do personagem/artista que quer vender:",
    "loja_busca_vazia": "Não encontrei cartas suas com <b>{busca}</b>.",
    "loja_custom_prompt": "Digite a quantidade de cartas que deseja vender:",
    "loja_vendido": "✅ Vendido: {quantidade}x <b>{carta}</b>. Você recebeu {quantidade} EccoCoins.\nSaldo: <b>{saldo}</b> 🫧",
    "loja_compra_prompt": "Cada giro custa 10 EccoCoins. Seu saldo permite até {max_giros} giro(s). Quantos quer comprar?",
    "loja_comprado": "✅ Comprou {quantidade} giro(s) por {preco} EccoCoins.\nSaldo: {saldo} 🫧 • Giros disponíveis: {giros} 🎲",
    "wishlist_titulo": "💧 <b>Wishlist de {jogador}</b>",
    "wishlist_vazia": "<i>A wishlist está vazia.</i>",
    "wishlist_adicionar": "Escolha uma carta para adicionar à wishlist:",
    "wishlist_remover": "Escolha uma carta para remover da wishlist:",
    "wishlist_busca": "Digite o nome de uma carta ou artista para buscar na wishlist:",
    "wishlist_sem_resultados": "Não encontrei cartas com <b>{busca}</b>.",
    "wishlist_resultados": "🔍 Resultados para <b>{busca}</b>:",
}

TEXTOS_META = {
    "start_texto": {
        "titulo": "🚀 Texto do /start",
        "vars": "{apelido}, {giros}, {dica_apelido}",
    },
    "comandos_texto": {
        "titulo": "📜 Texto do /comandos",
        "vars": "(sem variáveis obrigatórias)",
    },
    "menu_girar": {
        "titulo": "🎰 Cabeçalho do /girar",
        "vars": "{apelido}, {giros}",
    },
    "giro_resultado": {
        "titulo": "🌐 Resultado do Giro (1x)",
        "vars": "{apelido}, {carta_id}, {nome}, {tier_emoji}, {tier_curto}, {tier_nome}, {cat_codigo}, {local}, {qtd}, {tiers_obtidos}, {tiers_cadastrados}, {giros_restantes}",
    },
    "giro_multi": {
        "titulo": "🔥 Cabeçalho Multi-Giro",
        "vars": "{apelido}, {qtd_giros}, {giros_restantes}",
    },
    "giro_multi_item": {"titulo": "🔥 Linha de resultado Multi-Giro", "vars": "{tier_emoji}, {carta_id}, {nome}, {tier_curto}, {vezes_txt}, {tag_nova}"},
    "giro_rodape": {
        "titulo": "⬇️ Texto abaixo do Giro",
        "vars": "{apelido}, {qtd_giros}, {giros_restantes}",
    },
    "sem_giros": {
        "titulo": "⏳ Msg Sem Giros",
        "vars": "{apelido}",
    },
    "troca_proposta": {"titulo": "🔄 Proposta de Troca", "vars": "{troca_id}, {remetente}, {oferecida}, {tier_oferecida}, {pedida}, {tier_pedida}"},
    "troca_enviada": {"titulo": "✅ Troca enviada", "vars": "{destinatario}"},
    "troca_aceita": {"titulo": "✅ Troca aceita", "vars": "{troca_id}"},
    "troca_recusada": {"titulo": "❌ Troca recusada", "vars": "{troca_id}"},
    "troca_status_privado": {"titulo": "🔔 Atualização privada da troca", "vars": "{troca_id}, {status}"},
    "troca_status_grupo": {"titulo": "🔔 Aviso de troca no grupo", "vars": "{remetente}, {destinatario}, {troca_id}, {status}"},
    "doacao_prompt": {"titulo": "🎁 Menu de doação", "vars": "{destinatario}"},
    "doacao_quantidade": {"titulo": "🎁 Quantidade da doação", "vars": "{carta}, {quantidade}"},
    "doacao_recebida": {"titulo": "🎁 Carta recebida", "vars": "{quantidade}, {carta}, {remetente}"},
    "doacao_enviada": {"titulo": "🎁 Doação concluída", "vars": "{quantidade}, {carta}, {destinatario}"},
    "doacao_privado_enviada": {"titulo": "🎁 Confirmação privada de doação", "vars": "{quantidade}, {carta}, {destinatario}"},
    "doacao_grupo": {"titulo": "🎁 Aviso de doação no grupo", "vars": "{remetente}, {destinatario}, {quantidade}, {carta}"},
    "loja_menu": {"titulo": "🫧 Menu da loja", "vars": "{saldo}, {giros}"},
    "loja_venda_prompt": {"titulo": "💿 Escolher carta para vender", "vars": "(sem variáveis)"},
    "loja_venda_quantidade": {"titulo": "💿 Quantidade para vender", "vars": "{quantidade}, {carta}"},
    "loja_busca_prompt": {"titulo": "🔍 Buscar carta para vender", "vars": "(sem variáveis)"},
    "loja_busca_vazia": {"titulo": "🔍 Venda sem resultados", "vars": "{busca}"},
    "loja_custom_prompt": {"titulo": "✏️ Digitar quantidade da venda", "vars": "(sem variáveis)"},
    "loja_vendido": {"titulo": "💠 Venda concluída", "vars": "{quantidade}, {carta}, {saldo}"},
    "loja_compra_prompt": {"titulo": "🎲 Comprar giros", "vars": "{max_giros}"},
    "loja_comprado": {"titulo": "🎲 Giros comprados", "vars": "{quantidade}, {preco}, {saldo}, {giros}"},
    "wishlist_titulo": {"titulo": "💧 Título da Wishlist", "vars": "{jogador}"},
    "wishlist_vazia": {"titulo": "💧 Wishlist vazia", "vars": "(sem variáveis)"},
    "wishlist_adicionar": {"titulo": "💧 Adicionar à Wishlist", "vars": "(sem variáveis)"},
    "wishlist_remover": {"titulo": "💧 Remover da Wishlist", "vars": "(sem variáveis)"},
    "wishlist_busca": {"titulo": "🔍 Buscar cartas na Wishlist", "vars": "(sem variáveis)"},
    "wishlist_sem_resultados": {"titulo": "🔍 Busca sem resultados", "vars": "{busca}"},
    "wishlist_resultados": {"titulo": "🔍 Resultados da Wishlist", "vars": "{busca}"},
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
    ST_ASK_SUBSUB_CHOICE,
    ST_SUBSUB,
    ST_NEW_SUBSUB,
    ST_NOME,
    ST_NEW_NOME,
    ST_MIDIA,
    ST_TIER,
) = range(12)

# Estados do assistente /editcarta
(
    ED_MENU,
    ED_NOME,
    ED_CAT,
    ED_NEW_CAT,
    ED_ASK_SUB,
    ED_SUB,
    ED_NEW_SUB,
    ED_ASK_SUBSUB,
    ED_SUBSUB,
    ED_NEW_SUBSUB,
    ED_MIDIA,
    ED_TIER,
) = range(12, 24)

# Estados do gerenciador /categorias
(
    CM_HOME,
    CM_CAT_VIEW,
    CM_NEW_CAT,
    CM_REN_CAT,
    CM_SUB_VIEW,
    CM_NEW_SUB,
    CM_REN_SUB,
    CM_SUBSUB_VIEW,
    CM_NEW_SUBSUB,
    CM_REN_SUBSUB,
) = range(24, 34)

# Estados do gerenciador de textos públicos /textos
(
    TX_HOME,
    TX_VIEW,
    TX_EDIT_VAL,
    TX_START_MEDIA,
    TX_TIERS_MENU,
    TX_TIER_EDIT,
) = range(34, 40)

MV_ARTIST_NAME = 40

def esc(texto: str) -> str:
    return html.escape(str(texto), quote=False)

def _tags_por_carta(carta_ids: list[int]) -> dict[int, list[str]]:
    ids=list({int(cid) for cid in carta_ids if cid is not None})
    if not ids: return {}
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT ct.carta_id,ARRAY_AGG(t.nome ORDER BY LOWER(t.nome)) FROM carta_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.carta_id=ANY(%s) GROUP BY ct.carta_id",(ids,))
    result={cid:names for cid,names in cur.fetchall()}; cur.close(); conn.close()
    return result

def _texto_tags(carta_ids: list[int], tags_map: dict[int,list[str]] | None = None) -> str:
    tags_map=tags_map if tags_map is not None else _tags_por_carta(carta_ids)
    names=sorted({tag for cid in carta_ids for tag in tags_map.get(int(cid),[])},key=str.lower)
    return "🏷️ <b>Tags:</b> " + ", ".join(f"#{esc(name)}" for name in names) if names else ""

def texto_limpo_botao(html_str: str) -> str:
    """Remove tags HTML (como <tg-emoji>) para exibir em botões inline."""
    return re.sub(r"<[^>]+>", "", str(html_str))

def renderizar_template(template: str, **kwargs) -> str:
    resultado = template
    for k, v in kwargs.items():
        resultado = resultado.replace("{" + k + "}", str(v))
    return resultado

def formatar_local_carta(
    cid_cat: int,
    nome_cat: str,
    cid_sub: int | None,
    nome_sub: str | None,
    cid_subsub: int | None = None,
    nome_subsub: str | None = None,
) -> tuple[int, str]:
    """Retorna (codigo_exibicao, caminho_formatado) suportando até 3 níveis."""
    cat_codigo = cid_subsub if cid_subsub else (cid_sub if cid_sub else cid_cat)
    partes = [esc(nome_cat)]
    if nome_sub:
        partes.append(esc(nome_sub))
    if nome_subsub:
        partes.append(esc(nome_subsub))
    return cat_codigo, " • ".join(partes)

def extrair_html_com_custom_emojis(message) -> str:
    """Converte entidades de formatação do Telegram em HTML aceito pelo Bot API."""
    texto = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    if not texto:
        return ""
    if not entities:
        return texto

    utf16_bytes = texto.encode("utf-16-le")
    total_bytes = len(utf16_bytes)
    eventos = {}

    for ent in entities:
        inicio = max(0, min(total_bytes, ent.offset * 2))
        fim = max(inicio, min(total_bytes, (ent.offset + ent.length) * 2))
        tipo = ent.type
        abre = fecha = None
        if tipo == "bold":
            abre, fecha = "<b>", "</b>"
        elif tipo == "italic":
            abre, fecha = "<i>", "</i>"
        elif tipo == "underline":
            abre, fecha = "<u>", "</u>"
        elif tipo == "strikethrough":
            abre, fecha = "<s>", "</s>"
        elif tipo == "spoiler":
            abre, fecha = "<tg-spoiler>", "</tg-spoiler>"
        elif tipo == "blockquote":
            abre, fecha = "<blockquote>", "</blockquote>"
        elif tipo == "expandable_blockquote":
            abre, fecha = "<blockquote expandable>", "</blockquote>"
        elif tipo == "code":
            abre, fecha = "<code>", "</code>"
        elif tipo == "pre":
            language = getattr(ent, "language", "") or ""
            if language:
                abre = f'<pre><code class="language-{html.escape(language, quote=True)}">'
                fecha = "</code></pre>"
            else:
                abre, fecha = "<pre>", "</pre>"
        elif tipo == "text_link" and getattr(ent, "url", None):
            abre = f'<a href="{html.escape(ent.url, quote=True)}">'
            fecha = "</a>"
        elif tipo == "text_mention" and getattr(ent, "user", None):
            abre = f'<a href="tg://user?id={int(ent.user.id)}">'
            fecha = "</a>"
        elif tipo in ("url", "email"):
            valor = utf16_bytes[inicio:fim].decode("utf-16-le", errors="replace")
            href = f"mailto:{valor}" if tipo == "email" else valor
            abre = f'<a href="{html.escape(href, quote=True)}">'
            fecha = "</a>"
        elif tipo == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
            abre = f'<tg-emoji emoji-id="{html.escape(str(ent.custom_emoji_id), quote=True)}">'
            fecha = "</tg-emoji>"

        if abre is not None:
            tamanho = fim - inicio
            eventos.setdefault(inicio, {"fecha": [], "abre": []})["abre"].append((tamanho, abre))
            eventos.setdefault(fim, {"fecha": [], "abre": []})["fecha"].append((tamanho, fecha))

    partes = []
    cursor = 0
    for pos in sorted(eventos):
        if pos > cursor:
            partes.append(html.escape(utf16_bytes[cursor:pos].decode("utf-16-le", errors="replace"), quote=False))
        evento = eventos[pos]
        # Feche primeiro o intervalo mais interno e abra primeiro o maior.
        partes.extend(tag for _, tag in sorted(evento["fecha"], key=lambda item: item[0]))
        partes.extend(tag for _, tag in sorted(evento["abre"], key=lambda item: item[0], reverse=True))
        cursor = pos
    if cursor < total_bytes:
        partes.append(html.escape(utf16_bytes[cursor:].decode("utf-16-le", errors="replace"), quote=False))
    return "".join(partes)

# ==========================================
# POOL DE CONEXÕES & BANCO DE DADOS
# ==========================================
class _PooledConn:
    """Wrapper transparente que devolve a conexão ao pool ao chamar .close()."""
    def __init__(self, pool: ThreadedConnectionPool | None, conn, pooled: bool = True):
        self._pool = pool
        self._conn = conn
        self._pooled = pooled
        self._returned = False

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._returned:
            return
        self._returned = True
        if not self._pooled or self._pool is None:
            try:
                self._conn.close()
            except Exception:
                pass
            return
        try:
            if self._conn.closed != 0:
                self._pool.putconn(self._conn, close=True)
            else:
                self._conn.rollback()
                self._pool.putconn(self._conn)
        except Exception:
            try:
                self._pool.putconn(self._conn, close=True)
            except Exception:
                pass

def init_pool():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = ThreadedConnectionPool(
            minconn=1,
            maxconn=12,
            dsn=DATABASE_URL,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )

def get_db():
    init_pool()
    for _ in range(3):
        try:
            conn = DB_POOL.getconn()
        except PoolError:
            time.sleep(0.15)
            continue

        try:
            if conn.closed != 0:
                DB_POOL.putconn(conn, close=True)
                continue
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return _PooledConn(DB_POOL, conn, pooled=True)
        except Exception:
            try:
                DB_POOL.putconn(conn, close=True)
            except Exception:
                pass

    raw = psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    return _PooledConn(None, raw, pooled=False)

def carregar_cache_config():
    global _CONFIG_LOADED
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT chave, valor FROM config_textos")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    _CONFIG_CACHE.clear()
    for k, v in rows:
        _CONFIG_CACHE[k] = v
    _CONFIG_LOADED = True

def iniciar_banco():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            funcao TEXT NOT NULL DEFAULT 'admin' CHECK (funcao IN ('admin','editor'))
        );
        CREATE TABLE IF NOT EXISTS banimentos (
            user_id BIGINT PRIMARY KEY,
            aplicado_por BIGINT NOT NULL,
            criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS categorias (
            id SERIAL PRIMARY KEY,
            nome TEXT UNIQUE NOT NULL,
            usa_tiers BOOLEAN NOT NULL DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS subcategorias (
            id SERIAL PRIMARY KEY,
            categoria_id INTEGER NOT NULL REFERENCES categorias(id) ON DELETE CASCADE,
            nome TEXT NOT NULL,
            UNIQUE(categoria_id, nome)
        );
        CREATE TABLE IF NOT EXISTS subsubcategorias (
            id SERIAL PRIMARY KEY,
            subcategoria_id INTEGER NOT NULL REFERENCES subcategorias(id) ON DELETE CASCADE,
            nome TEXT NOT NULL,
            UNIQUE(subcategoria_id, nome)
        );
        CREATE TABLE IF NOT EXISTS molduras (
            tier INTEGER PRIMARY KEY CHECK (tier BETWEEN 1 AND 6),
            png_bytes BYTEA NOT NULL
        );
        CREATE TABLE IF NOT EXISTS config_textos (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            nome TEXT,
            apelido TEXT,
            giros_restantes INTEGER DEFAULT 10,
            ultimo_reset TEXT,
            aqua_coins INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cartas (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            categoria_id INTEGER NOT NULL REFERENCES categorias(id),
            subcategoria_id INTEGER REFERENCES subcategorias(id),
            subsubcategoria_id INTEGER REFERENCES subsubcategorias(id),
            tier INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 6),
            peso_drop INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'photo',
            original_file_id TEXT,
            event_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS tags (
            id SERIAL PRIMARY KEY,
            nome TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS carta_tags (
            carta_id INTEGER NOT NULL REFERENCES cartas(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (carta_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS pedidos_casamento (
            id SERIAL PRIMARY KEY,
            proponente_id BIGINT NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
            destinatario_id BIGINT NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
            chat_id BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente','aceito','recusado','cancelado')),
            criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS casamentos (
            id SERIAL PRIMARY KEY,
            usuario_a BIGINT NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
            usuario_b BIGINT NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
            chat_id BIGINT,
            ativo BOOLEAN NOT NULL DEFAULT TRUE,
            criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            encerrado_em TIMESTAMPTZ,
            CHECK (usuario_a < usuario_b)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_casamento_ativo_a ON casamentos(usuario_a) WHERE ativo;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_casamento_ativo_b ON casamentos(usuario_b) WHERE ativo;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pedido_casamento_pendente ON pedidos_casamento(proponente_id,destinatario_id) WHERE status='pendente';
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
        CREATE TABLE IF NOT EXISTS wishlists (
            user_id BIGINT NOT NULL REFERENCES usuarios(user_id) ON DELETE CASCADE,
            carta_id INTEGER NOT NULL REFERENCES cartas(id) ON DELETE CASCADE,
            criada_em TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, carta_id)
        );
    """)

    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS aqua_coins INTEGER NOT NULL DEFAULT 0;")
    cur.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS funcao TEXT NOT NULL DEFAULT 'admin';")
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS bio TEXT NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS xp BIGINT NOT NULL DEFAULT 0;")
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS xp_ultimo_em TIMESTAMPTZ;")
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS musica_favorita TEXT NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS carta_favorita_id INTEGER REFERENCES cartas(id) ON DELETE SET NULL;")
    cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS casado_com BIGINT REFERENCES usuarios(user_id) ON DELETE SET NULL;")
    cur.execute("ALTER TABLE trocas ADD COLUMN IF NOT EXISTS origin_chat_id BIGINT;")
    cur.execute("ALTER TABLE cartas DROP CONSTRAINT IF EXISTS cartas_tier_check;")
    cur.execute("ALTER TABLE cartas ADD CONSTRAINT cartas_tier_check CHECK (tier BETWEEN 1 AND 6);")
    cur.execute("ALTER TABLE molduras DROP CONSTRAINT IF EXISTS molduras_tier_check;")
    cur.execute("ALTER TABLE molduras ADD CONSTRAINT molduras_tier_check CHECK (tier BETWEEN 1 AND 6);")
    cur.execute("ALTER TABLE cartas ADD COLUMN IF NOT EXISTS original_file_id TEXT;")
    cur.execute("ALTER TABLE cartas ADD COLUMN IF NOT EXISTS event_id INTEGER;")
    cur.execute("UPDATE cartas SET original_file_id = file_id WHERE original_file_id IS NULL;")

    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'categorias' AND column_name = 'usa_tiers'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE categorias ADD COLUMN usa_tiers BOOLEAN NOT NULL DEFAULT TRUE;")
        cur.execute("""
            UPDATE categorias SET usa_tiers = FALSE
            WHERE LOWER(TRIM(nome)) IN ('animes', 'séries', 'series', 'filmes', 'variedades')
        """)

    # Migração segura: coluna apelido em usuarios
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'usuarios' AND column_name = 'apelido'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE usuarios ADD COLUMN apelido TEXT;")

    # Migração segura: coluna subsubcategoria_id em cartas (3º nível para grupos de K-pop, etc.)
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'cartas' AND column_name = 'subsubcategoria_id'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE cartas ADD COLUMN subsubcategoria_id INTEGER REFERENCES subsubcategorias(id);")

    cur.execute("""
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'usuarios' AND indexname = 'idx_usuarios_apelido_unico'
    """)
    if not cur.fetchone():
        cur.execute("""
            CREATE UNIQUE INDEX idx_usuarios_apelido_unico
                ON usuarios (LOWER(TRIM(apelido)))
                WHERE apelido IS NOT NULL AND TRIM(apelido) <> '';
        """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_inventario_user_carta ON inventario (user_id, carta_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cartas_cat_sub_subsub ON cartas (categoria_id, subcategoria_id, subsubcategoria_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cartas_lower_nome ON cartas (LOWER(nome));")

    cur.execute("SELECT COUNT(*) FROM categorias")
    if cur.fetchone()[0] == 0:
        for cat_nome, subs in CATALOGO_INICIAL.items():
            nomes_sem_tiers = {"animes", "séries", "series", "filmes", "variedades"}
            usa_tiers = cat_nome.strip().lower() not in nomes_sem_tiers
            cur.execute("INSERT INTO categorias (nome, usa_tiers) VALUES (%s, %s) RETURNING id", (cat_nome, usa_tiers))
            cat_id = cur.fetchone()[0]
            for sub_nome in subs:
                cur.execute(
                    "INSERT INTO subcategorias (categoria_id, nome) VALUES (%s, %s)",
                    (cat_id, sub_nome)
                )
    else:
        # Garante que a subcategoria K-pop exista dentro de Música mesmo em bancos já iniciados
        cur.execute("SELECT id FROM categorias WHERE LOWER(nome) IN ('música', 'musica') LIMIT 1")
        r_mus = cur.fetchone()
        if r_mus:
            mus_id = r_mus[0]
            cur.execute(
                "SELECT 1 FROM subcategorias WHERE categoria_id = %s AND LOWER(nome) IN ('k-pop', 'kpop') LIMIT 1",
                (mus_id,)
            )
            if not cur.fetchone():
                cur.execute("INSERT INTO subcategorias (categoria_id, nome) VALUES (%s, %s)", (mus_id, "K-pop"))

    from eventos import criar_schema_eventos
    criar_schema_eventos(conn)

    conn.commit()
    cur.close()
    conn.close()
    carregar_cache_config()

def obter_config_valor(chave: str, padrao: str = "") -> str:
    if not _CONFIG_LOADED:
        carregar_cache_config()
    return _CONFIG_CACHE.get(chave, padrao)

def salvar_config_valor(chave: str, valor: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO config_textos (chave, valor) VALUES (%s, %s)
        ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
    """, (chave, valor))
    conn.commit()
    cur.close()
    conn.close()
    _CONFIG_CACHE[chave] = valor

def remover_config_valor(chave: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM config_textos WHERE chave = %s", (chave,))
    conn.commit()
    cur.close()
    conn.close()
    _CONFIG_CACHE.pop(chave, None)

def obter_texto_publico(chave: str) -> str:
    return obter_config_valor(chave, TEXTOS_PADRAO.get(chave, ""))

def obter_emoji_tier(tier: int) -> str:
    return obter_config_valor(f"tier_emoji_{tier}", TIERS_CONFIG[tier]["emoji"])

def categoria_usa_tiers(cat_id: int) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT usa_tiers FROM categorias WHERE id = %s", (cat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return bool(row[0]) if row else True

def eh_admin(user_id: int) -> bool:
    if user_id in SUPER_ADMINS:
        return True
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id = %s AND funcao = 'admin'", (user_id,))
    achou = cur.fetchone() is not None
    cur.close()
    conn.close()
    return achou

def eh_editor(user_id: int) -> bool:
    if user_id in SUPER_ADMINS:
        return True
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT funcao FROM admins WHERE user_id=%s", (user_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    return bool(row and row[0] in ("admin", "editor"))

def _hoje_sao_paulo() -> date:
    return datetime.now(FUSO_GIROS).date()

def _dias_desde_reposicao(ultimo_reset, hoje: date) -> int:
    try:
        ultima_data = date.fromisoformat(str(ultimo_reset))
    except (TypeError, ValueError):
        return 1
    return max(0, (hoje - ultima_data).days)

def repor_giros_diarios():
    """Adds one daily allotment at São Paulo midnight, catching up missed dates."""
    hoje = _hoje_sao_paulo()
    hoje_txt = hoje.isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, ultimo_reset FROM usuarios WHERE ultimo_reset IS DISTINCT FROM %s FOR UPDATE",
        (hoje_txt,),
    )
    for user_id, ultimo_reset in cur.fetchall():
        dias = _dias_desde_reposicao(ultimo_reset, hoje)
        if dias:
            cur.execute(
                "UPDATE usuarios SET giros_restantes = COALESCE(giros_restantes, 0) + %s, ultimo_reset = %s WHERE user_id = %s",
                (dias * GIROS_DIARIOS, hoje_txt, user_id),
            )
    conn.commit()
    cur.close()
    conn.close()

async def loop_reposicao_diaria():
    while True:
        agora = datetime.now(FUSO_GIROS)
        amanha = agora.date() + timedelta(days=1)
        meia_noite = datetime.combine(amanha, datetime_time.min, tzinfo=FUSO_GIROS)
        await asyncio.sleep(max(1, (meia_noite - agora).total_seconds()))
        try:
            await asyncio.to_thread(repor_giros_diarios)
        except Exception as e:
            print(f"⚠️ Falha ao repor giros diários: {e}", flush=True)

def verificar_usuario(user_id: int, username: str = "") -> int:
    conn = get_db()
    cur = conn.cursor()
    hoje_data = _hoje_sao_paulo()
    hoje = hoje_data.isoformat()
    username_limpo = (username or "").lstrip("@").lower()

    cur.execute(
        "INSERT INTO usuarios (user_id, username, giros_restantes, ultimo_reset) VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, username_limpo, GIROS_DIARIOS, hoje),
    )
    cur.execute("SELECT giros_restantes, ultimo_reset, username FROM usuarios WHERE user_id = %s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    giros, ultimo_reset, uname_atual = row
    if username_limpo and username_limpo != (uname_atual or ""):
        cur.execute("UPDATE usuarios SET username = %s WHERE user_id = %s", (username_limpo, user_id))
    dias = _dias_desde_reposicao(ultimo_reset, hoje_data)
    if dias:
        giros = (giros or 0) + dias * GIROS_DIARIOS
        cur.execute(
            "UPDATE usuarios SET giros_restantes = %s, ultimo_reset = %s WHERE user_id = %s",
            (giros, hoje, user_id),
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

def _compor_imagem_com_moldura(image_bytes: bytes, moldura_bytes: bytes | None) -> io.BytesIO:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    base = ImageOps.fit(img, (LARGURA_CARTA, ALTURA_CARTA), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    if moldura_bytes:
        moldura = Image.open(io.BytesIO(bytes(moldura_bytes))).convert("RGBA")
        if moldura.size != (LARGURA_CARTA, ALTURA_CARTA):
            moldura = moldura.resize((LARGURA_CARTA, ALTURA_CARTA), Image.Resampling.LANCZOS)
        final_img = Image.alpha_composite(base, moldura)
    else:
        final_img = base

    saida = io.BytesIO()
    final_img.convert("RGB").save(saida, format="JPEG", quality=92)
    saida.seek(0)
    return saida

def processar_imagem_carta(image_bytes: bytes, tier: int) -> io.BytesIO:
    """Padroniza a carta e aplica a moldura cadastrada para o tier, se houver."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT png_bytes FROM molduras WHERE tier = %s", (tier,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return _compor_imagem_com_moldura(image_bytes, row[0] if row else None)

async def _regenerar_cartas_moldura_tier(context, tier: int, moldura_bytes: bytes | None, chat_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, original_file_id, media_type FROM cartas WHERE tier=%s AND event_id IS NULL AND media_type='photo'", (tier,))
    cartas = cur.fetchall()
    cur.close()
    conn.close()
    atualizadas = 0
    for carta_id, original_id, media_type in cartas:
        if not original_id:
            continue
        try:
            tg_file = await context.bot.get_file(original_id)
            original = io.BytesIO()
            await tg_file.download_to_memory(original)
            resultado = await asyncio.to_thread(_compor_imagem_com_moldura, original.getvalue(), moldura_bytes)
            enviada = await context.bot.send_photo(chat_id=chat_id, photo=resultado)
            novo_file_id = enviada.photo[-1].file_id
            await enviada.delete()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("UPDATE cartas SET file_id=%s WHERE id=%s", (novo_file_id, carta_id))
            conn.commit()
            cur.close()
            conn.close()
            atualizadas += 1
        except Exception as exc:
            print(f"Falha ao atualizar moldura da carta #{carta_id}: {exc}", flush=True)
    return atualizadas

async def _regenerar_cartas_moldura_evento(context, event_id: int, moldura_bytes: bytes, chat_id: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id,original_file_id FROM cartas WHERE event_id=%s AND media_type='photo'", (event_id,))
    cards = cur.fetchall(); cur.close(); conn.close()
    atualizadas = 0
    for card_id, original_id in cards:
        if not original_id:
            continue
        try:
            tg_file = await context.bot.get_file(original_id)
            original = io.BytesIO(); await tg_file.download_to_memory(original)
            resultado = await asyncio.to_thread(_compor_imagem_com_moldura, original.getvalue(), moldura_bytes)
            enviada = await context.bot.send_photo(chat_id=chat_id, photo=resultado)
            novo_file_id = enviada.photo[-1].file_id; await enviada.delete()
            conn = get_db(); cur = conn.cursor()
            cur.execute("UPDATE cartas SET file_id=%s WHERE id=%s", (novo_file_id, card_id))
            conn.commit(); cur.close(); conn.close(); atualizadas += 1
        except Exception as exc:
            print(f"Falha ao atualizar moldura do evento na carta #{card_id}: {exc}", flush=True)
    return atualizadas

async def enviar_midia_carta(message, file_id: str, media_type: str, legenda: str, bot=None):
    """Envia a foto/GIF da carta com fallback seguro para grupos com restrições."""
    try:
        if media_type == "animation":
            await message.reply_animation(animation=file_id, caption=legenda, parse_mode="HTML")
        else:
            await message.reply_photo(photo=file_id, caption=legenda, parse_mode="HTML")
        return
    except Exception:
        pass

    bot_ref = bot or message.get_bot()
    chat_id = message.chat_id
    thread_id = getattr(message, "message_thread_id", None)
    kwargs = {"chat_id": chat_id, "caption": legenda, "parse_mode": "HTML"}
    if thread_id:
        kwargs["message_thread_id"] = thread_id

    try:
        if media_type == "animation":
            await bot_ref.send_animation(animation=file_id, **kwargs)
        else:
            await bot_ref.send_photo(photo=file_id, **kwargs)
    except Exception:
        text_kwargs = {"chat_id": chat_id, "text": legenda, "parse_mode": "HTML"}
        if thread_id:
            text_kwargs["message_thread_id"] = thread_id
        await bot_ref.send_message(**text_kwargs)

async def enviar_texto_seguro(chat_id: int, texto: str, bot, thread_id=None, reply_to_message=None):
    """Envia mensagem de texto no chat/tópico mesmo se a mensagem original tiver sido apagada."""
    if reply_to_message:
        try:
            await reply_to_message.reply_text(texto, parse_mode="HTML")
            return
        except Exception:
            pass

    kwargs = {"chat_id": chat_id, "text": texto, "parse_mode": "HTML"}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    await bot.send_message(**kwargs)

# ==========================================
# COMANDOS DE JOGADORES COMUNS
# ==========================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    giros = verificar_usuario(user.id, user.username)
    apelido, tem_apelido = obter_apelido(user.id, user.username)

    dica_apelido = (
        "\n\n💡 <i>Dica: use <code>/apelido SeuNome</code> para personalizar como seu nome aparece nas cartas.</i>"
        if not tem_apelido else ""
    )

    template_start = obter_texto_publico("start_texto")
    texto = renderizar_template(
        template_start,
        apelido=esc(apelido),
        giros=giros,
        dica_apelido=dica_apelido,
        studio="",
    )

    media_id = obter_config_valor("start_media_id", "")
    media_type = obter_config_valor("start_media_type", "animation")

    if media_id:
        if len(texto) <= 1024:
            await enviar_midia_carta(msg, media_id, media_type, texto, bot=context.bot)
        else:
            await enviar_midia_carta(msg, media_id, media_type, "", bot=context.bot)
            await msg.reply_text(texto, parse_mode="HTML")
    else:
        await msg.reply_text(texto, parse_mode="HTML")

async def cmd_comandos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    verificar_usuario(user.id, user.username)
    texto = obter_texto_publico("comandos_texto")
    comandos_novos = [
        ("/tag", "• /tag nome — Ver Minha coleção e Catálogo filtrados por tag"),
        ("/col", "• /col tag — Ver sua coleção filtrada por tag"),
        ("/casar", "• /casar @pessoa — Propor casamento para outro jogador"),
        ("/divorciar", "• /divorciar — Encerrar seu casamento atual"),
        ("/perfil", "• /perfil [@pessoa] — Ver e editar seu perfil, XP e carta favorita"),
        ("/carta", "• /carta ID ou nome — Ver uma carta e consultar quantas cópias você possui"),
        ("/eventos", "• /eventos — Abrir eventos ativos e suas cartas especiais"),
        ("/natalpresente", "• /natalpresente @pessoa — Presentear cartas do evento de Natal"),
        ("/trocar", "• /trocar <code>@pessoa</code> ou apelido — Propor troca escolhendo cartas pelos botões"),
        ("/doar", "• /doar <code>@pessoa</code> ou apelido — Doar cartas com botões"),
        ("/wishlist", "• /wishlist [pessoa] — Editar sua wishlist ou consultar a de alguém"),
        ("/loja", "• /loja — Vender cartas por EccoCoins e comprar giros"),
    ]
    linhas_ausentes = [
        linha for comando, linha in comandos_novos
        if (comando not in texto or (comando == "/trocar" and "@pessoa" not in texto) or (comando == "/carta" and "ou nome" not in texto) or (comando == "/col" and "/col tag" not in texto) or (comando == "/tag" and "/tag nome" not in texto))
    ]
    if linhas_ausentes:
        texto = f"{texto.rstrip()}\n" + "\n".join(linhas_ausentes)
    await msg.reply_text(texto, parse_mode="HTML")

async def cmd_apelido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    verificar_usuario(user.id, user.username)

    novo_apelido = " ".join(" ".join(context.args).split()).strip()
    if not novo_apelido:
        atual, _ = obter_apelido(user.id, user.username)
        await msg.reply_text(
            f"🫧 Seu nome atual no jogo é: <b>{esc(atual)}</b>\n\n"
            f"Para alterar, envie: <code>/apelido Seu Novo Nome</code>",
            parse_mode="HTML"
        )
        return

    if len(novo_apelido) < 2 or len(novo_apelido) > 32:
        await msg.reply_text("⚠️ O apelido precisa ter entre <b>2 e 32</b> caracteres.", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM usuarios WHERE LOWER(TRIM(apelido)) = LOWER(%s) AND user_id != %s LIMIT 1",
        (novo_apelido, user.id)
    )
    if cur.fetchone():
        cur.close()
        conn.close()
        await msg.reply_text(
            f"⚠️ O apelido <b>{esc(novo_apelido)}</b> já está sendo usado por outro jogador. Escolha outro!",
            parse_mode="HTML"
        )
        return

    try:
        cur.execute("UPDATE usuarios SET apelido = %s WHERE user_id = %s", (novo_apelido, user.id))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        cur.close()
        conn.close()
        await msg.reply_text(
            f"⚠️ O apelido <b>{esc(novo_apelido)}</b> já está sendo usado por outro jogador. Escolha outro!",
            parse_mode="HTML"
        )
        return

    cur.close()
    conn.close()

    await msg.reply_text(
        f"✅ Apelido atualizado para: 🫧 <b>{esc(novo_apelido)}</b>",
        parse_mode="HTML"
    )

async def _buscar_usuario_perfil(identificador: str):
    valor = identificador.strip()
    if not valor:
        return None
    valor_sem_arroba = valor.lstrip("@").strip()
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM usuarios WHERE user_id::text=%s OR LOWER(username)=LOWER(%s) OR LOWER(TRIM(apelido))=LOWER(%s) LIMIT 1", (valor_sem_arroba, valor_sem_arroba, valor))
    row = cur.fetchone(); cur.close(); conn.close()
    return row[0] if row else None

async def _propor_casamento(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int):
    proposer=update.effective_user; msg=update.effective_message
    if not proposer or not msg: return
    if target_id==proposer.id:
        await msg.reply_text("Você não pode propor casamento a si mesmo."); return
    verificar_usuario(proposer.id,proposer.username)
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT user_id,casado_com FROM usuarios WHERE user_id=ANY(%s) ORDER BY user_id",([proposer.id,target_id],)); rows=cur.fetchall()
    married={uid:partner for uid,partner in rows}
    if target_id not in married:
        cur.close(); conn.close(); await msg.reply_text("Essa pessoa precisa iniciar o bot antes de receber propostas."); return
    if married.get(proposer.id) or married.get(target_id):
        cur.close(); conn.close(); await msg.reply_text("Uma das pessoas já está em um casamento. Para encerrar, use /divorciar."); return
    cur.execute("SELECT id FROM pedidos_casamento WHERE proponente_id=%s AND destinatario_id=%s AND status='pendente'",(proposer.id,target_id)); pending=cur.fetchone()
    if pending:
        cur.close(); conn.close(); await msg.reply_text("Você já enviou uma proposta que ainda está aguardando resposta."); return
    cur.execute("INSERT INTO pedidos_casamento(proponente_id,destinatario_id,chat_id) VALUES(%s,%s,%s) RETURNING id",(proposer.id,target_id,msg.chat_id)); proposal_id=cur.fetchone()[0]
    cur.execute("SELECT apelido,username FROM usuarios WHERE user_id=%s",(proposer.id,)); who=cur.fetchone(); conn.commit(); cur.close(); conn.close()
    sender=(who[0] or (f"@{who[1]}" if who and who[1] else f"Mixer #{str(proposer.id)[-4:]}")) if who else "Alguém"
    keyboard=InlineKeyboardMarkup([[InlineKeyboardButton("💍 Aceitar",callback_data=f"casar|aceitar|{proposal_id}"),InlineKeyboardButton("🙅 Recusar",callback_data=f"casar|recusar|{proposal_id}")]])
    text=f"💌 <b>Proposta de casamento</b>\n{esc(sender)} quer se casar com você."
    try:
        await context.bot.send_message(chat_id=target_id,text=text,reply_markup=keyboard,parse_mode="HTML")
        await msg.reply_text("💌 Proposta enviada no privado. A pessoa pode aceitar ou recusar por lá.")
    except Exception:
        await context.bot.send_message(chat_id=msg.chat_id,text=text,reply_markup=keyboard,parse_mode="HTML")
        await msg.reply_text("💌 Não consegui enviar no privado; deixei a proposta aqui no chat.")

async def cmd_casar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message; user=update.effective_user
    if not msg or not user: return
    alvo=" ".join(context.args).strip()
    if not alvo and msg.reply_to_message and msg.reply_to_message.from_user:
        alvo=str(msg.reply_to_message.from_user.id)
    if not alvo:
        await msg.reply_text("Use <code>/casar @pessoa</code> ou responda à mensagem dela com <code>/casar</code>.",parse_mode="HTML"); return
    target_id=await _buscar_usuario_perfil(alvo)
    if not target_id:
        await msg.reply_text("Não encontrei essa pessoa. Ela precisa iniciar o bot com /start."); return
    await _propor_casamento(update,context,target_id)

async def callback_casamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; _,action,proposal_raw=q.data.split("|"); proposal_id=int(proposal_raw)
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT proponente_id,destinatario_id,chat_id,status FROM pedidos_casamento WHERE id=%s FOR UPDATE",(proposal_id,)); proposal=cur.fetchone()
    if not proposal:
        conn.rollback(); cur.close(); conn.close(); await q.answer("Proposta não encontrada.",show_alert=True); return
    proposer,target,origin_chat,status=proposal
    if q.from_user.id!=target:
        conn.rollback(); cur.close(); conn.close(); await q.answer("Só o destinatário pode responder a esta proposta.",show_alert=True); return
    if status!="pendente":
        conn.rollback(); cur.close(); conn.close(); await q.answer("Essa proposta já foi respondida.",show_alert=True); return
    if action=="recusar":
        cur.execute("UPDATE pedidos_casamento SET status='recusado' WHERE id=%s",(proposal_id,)); conn.commit(); cur.close(); conn.close()
        await q.answer("Proposta recusada."); await q.edit_message_text("🙅 Você recusou a proposta de casamento.")
        texto="🙅 A proposta de casamento foi recusada."
    else:
        cur.execute("SELECT user_id,casado_com FROM usuarios WHERE user_id=ANY(%s) ORDER BY user_id FOR UPDATE",([proposer,target],)); users=cur.fetchall(); pairs={uid:partner for uid,partner in users}
        if len(users)<2 or pairs.get(proposer) or pairs.get(target):
            cur.execute("UPDATE pedidos_casamento SET status='cancelado' WHERE id=%s",(proposal_id,)); conn.commit(); cur.close(); conn.close()
            await q.answer("Uma das pessoas já se casou; proposta cancelada.",show_alert=True); await q.edit_message_text("Essa proposta foi cancelada porque uma das pessoas já está casada."); return
        a,b=sorted((proposer,target))
        cur.execute("UPDATE usuarios SET casado_com=CASE WHEN user_id=%s THEN %s ELSE %s END WHERE user_id=ANY(%s)",(proposer,target,proposer,[proposer,target]))
        cur.execute("INSERT INTO casamentos(usuario_a,usuario_b,chat_id) VALUES(%s,%s,%s)",(a,b,origin_chat))
        cur.execute("UPDATE pedidos_casamento SET status='aceito' WHERE id=%s",(proposal_id,)); conn.commit(); cur.close(); conn.close()
        await q.answer("Vocês agora estão casados! 💍"); await q.edit_message_text("💍 Proposta aceita! Vocês agora estão casados.")
        texto="💍 <b>Casamento confirmado!</b> Parabéns ao novo casal!"
    await _notificar_casamento(context.bot,proposer,target,origin_chat,texto)

async def _notificar_casamento(bot, user_a: int, user_b: int, chat_id: int | None, texto: str):
    for uid in {user_a,user_b}:
        try: await bot.send_message(chat_id=uid,text=texto,parse_mode="HTML")
        except Exception: pass
    if chat_id and chat_id not in {user_a,user_b}:
        try: await bot.send_message(chat_id=chat_id,text=texto,parse_mode="HTML")
        except Exception: pass

async def cmd_divorciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; msg=update.effective_message
    if not user or not msg: return
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT casado_com FROM usuarios WHERE user_id=%s FOR UPDATE",(user.id,)); row=cur.fetchone()
    partner=row[0] if row else None
    if not partner:
        conn.rollback(); cur.close(); conn.close(); await msg.reply_text("Seu perfil não mostra um casamento ativo."); return
    a,b=sorted((user.id,partner)); cur.execute("SELECT chat_id FROM casamentos WHERE usuario_a=%s AND usuario_b=%s AND ativo=TRUE ORDER BY id DESC LIMIT 1",(a,b)); marriage=cur.fetchone(); group_chat=marriage[0] if marriage else msg.chat_id
    cur.execute("UPDATE usuarios SET casado_com=NULL WHERE user_id=ANY(%s)",([user.id,partner],))
    cur.execute("UPDATE casamentos SET ativo=FALSE,encerrado_em=NOW() WHERE usuario_a=%s AND usuario_b=%s AND ativo=TRUE",(a,b)); conn.commit(); cur.close(); conn.close()
    await msg.reply_text("💔 Casamento encerrado.")
    await _notificar_casamento(context.bot,user.id,partner,group_chat,"💔 O casamento foi encerrado.")

async def _renderizar_perfil(user_id: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT u.apelido,u.username,u.bio,u.xp,u.musica_favorita,u.casado_com,
               COALESCE((SELECT SUM(i.quantidade) FROM inventario i WHERE i.user_id=u.user_id),0),
               fc.id,fc.nome,fc.tier,par.apelido,par.username
        FROM usuarios u
        LEFT JOIN cartas fc ON fc.id=u.carta_favorita_id
        LEFT JOIN usuarios par ON par.user_id=u.casado_com
        WHERE u.user_id=%s
    """, (user_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        return None, None
    apelido, username, bio, xp, musica, parceiro_id, total_cartas, carta_id, carta_nome, carta_tier, parceiro_apelido, parceiro_username = row
    display = (apelido or "").strip() or (f"@{username}" if username else f"Mixer #{str(user_id)[-4:]}")
    nivel = int(xp or 0) // 100 + 1
    progresso = int(xp or 0) % 100
    parceiro = (parceiro_apelido or (f"@{parceiro_username}" if parceiro_username else "Jogador")) if parceiro_id else "Solteiro(a)"
    carta = f"{carta_nome} [Tier {carta_tier}]" if carta_nome else "Não definida"
    carta_tags=_texto_tags([carta_id]) if carta_id else ""
    texto = (
        f"⠀⢏⢢⣀⡔⡹  ⊹   <b>{esc(display)}</b>\n"
        f"💧 : {esc(bio.strip() if bio and bio.strip() else 'Bio não definida')}\n"
        f"♻️ : {int(total_cartas or 0)} cartas no inventário\n"
        f"🖥 : Nível {nivel} · {int(xp or 0)} XP ({progresso}/100 para o próximo nível)\n"
        f"🌱 : {esc(musica.strip() if musica and musica.strip() else 'Música favorita não definida')}\n"
        f"🫧 : {esc(parceiro)}\n\n"
        f"🖱 : {esc(carta)}" + (f"\n{carta_tags}" if carta_tags else "")
    )
    return texto, carta_nome

async def _mostrar_perfil(target, context, user_id: int, permitir_edicao: bool):
    texto, _ = await _renderizar_perfil(user_id)
    if texto is None:
        if hasattr(target, "reply_text"):
            await target.reply_text("Ainda não encontrei esse perfil. A pessoa precisa iniciar o bot com /start.")
        else:
            await target.edit_message_text("Ainda não encontrei esse perfil. A pessoa precisa iniciar o bot com /start.")
        return
    markup = None
    if permitir_edicao:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("💧 Editar bio", callback_data="perfil|editar|bio"), InlineKeyboardButton("🌱 Música favorita", callback_data="perfil|editar|musica")],
            [InlineKeyboardButton("🖱 Escolher carta favorita", callback_data="perfil|buscarcarta")],
        ])
    else:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("💍 Propor casamento",callback_data=f"perfil|casar|{user_id}")]])
    if hasattr(target, "reply_text"):
        await target.reply_text(texto, parse_mode="HTML", reply_markup=markup)
    else:
        await target.edit_message_text(texto, parse_mode="HTML", reply_markup=markup)

async def cmd_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if not msg or not user:
        return
    verificar_usuario(user.id, user.username)
    target_id = user.id
    identificador = " ".join(context.args).strip()
    if not identificador and msg.reply_to_message and msg.reply_to_message.from_user:
        alvo = msg.reply_to_message.from_user
        target_id = alvo.id
        verificar_usuario(alvo.id, alvo.username)
    elif identificador:
        encontrado = await _buscar_usuario_perfil(identificador)
        if not encontrado:
            await msg.reply_text("Não encontrei esse jogador. Ele precisa iniciar o bot com /start.")
            return
        target_id = encontrado
    context.user_data["perfil_visualizado"] = target_id
    await _mostrar_perfil(msg, context, target_id, target_id == user.id)

async def callback_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; partes = q.data.split("|")
    dono = context.user_data.get("perfil_visualizado")
    if partes[1]=="casar":
        target_id=int(partes[2])
        if dono!=target_id or target_id==q.from_user.id:
            await q.answer("Abra o perfil da pessoa que deseja convidar.",show_alert=True); return
        await q.answer(); await _propor_casamento(update,context,target_id); return
    if dono != q.from_user.id:
        await q.answer("Abra seu próprio /perfil para editar.", show_alert=True); return
    acao = partes[1]
    if acao == "cancelar":
        context.user_data.pop("perfil_pendente", None)
        await q.answer("Busca cancelada.")
        await q.edit_message_reply_markup(reply_markup=None)
        return
    if acao == "editar":
        campo = partes[2]
        context.user_data["perfil_pendente"] = campo
        await q.answer()
        prompt = "Envie sua bio (até 160 caracteres). Digite limpar para apagar." if campo == "bio" else "Envie o nome da sua música favorita (até 100 caracteres). Digite limpar para apagar."
        await q.message.reply_text(prompt)
        return
    if acao == "buscarcarta":
        context.user_data["perfil_pendente"] = "carta_busca"
        await q.answer()
        await q.message.reply_text("Digite o nome da carta para pesquisar.")
        return
    if acao == "carta":
        carta_id = int(partes[2]); conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE usuarios SET carta_favorita_id=%s WHERE user_id=%s", (carta_id, dono)); conn.commit(); cur.close(); conn.close()
        await q.answer("Carta favorita atualizada!")
        await _mostrar_perfil(q.message, context, dono, True)

async def _handler_texto_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pendente = context.user_data.get("perfil_pendente"); msg = update.effective_message
    if not pendente or not msg or not msg.text:
        return False
    valor = "" if msg.text.strip().lower() == "limpar" else msg.text.strip()
    if pendente == "carta_busca":
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT id,nome,tier FROM cartas WHERE nome ILIKE %s ORDER BY nome,tier,id LIMIT 10", (f"%{valor}%",))
        rows = cur.fetchall(); cur.close(); conn.close()
        if not rows:
            await msg.reply_text("Não encontrei cartas com esse nome. Tente outra busca."); return True
        botoes = [[InlineKeyboardButton(f"{nome} · Tier {tier}", callback_data=f"perfil|carta|{cid}")] for cid,nome,tier in rows]
        botoes.append([InlineKeyboardButton("Cancelar", callback_data="perfil|cancelar")])
        await msg.reply_text("Escolha sua carta favorita:", reply_markup=InlineKeyboardMarkup(botoes))
        return True
    limite = 160 if pendente == "bio" else 100
    if len(valor) > limite:
        await msg.reply_text(f"O limite é {limite} caracteres. Envie um texto menor."); return True
    coluna = "bio" if pendente == "bio" else "musica_favorita"
    conn = get_db(); cur = conn.cursor()
    cur.execute(f"UPDATE usuarios SET {coluna}=%s WHERE user_id=%s", (valor, update.effective_user.id)); conn.commit(); cur.close(); conn.close()
    context.user_data.pop("perfil_pendente", None)
    await msg.reply_text("Perfil atualizado.")
    await _mostrar_perfil(msg, context, update.effective_user.id, True)
    return True

async def handler_xp_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; user = update.effective_user
    if not msg or not user or user.is_bot or msg.chat.type not in ("group", "supergroup"):
        return
    verificar_usuario(user.id, user.username)
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE usuarios SET xp=COALESCE(xp,0)+1,xp_ultimo_em=NOW() WHERE user_id=%s AND (xp_ultimo_em IS NULL OR xp_ultimo_em < NOW()-INTERVAL '1 minute')", (user.id,))
    conn.commit(); cur.close(); conn.close()

async def cmd_meu_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg and user:
        await msg.reply_text(f"Seu ID numérico é: <code>{user.id}</code>", parse_mode="HTML")

# ==========================================
# SISTEMA DE GIROS (1x, MÚLTIPLO E GASTAR TUDO — 3 NÍVEIS)
# ==========================================
async def cmd_girar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat:
        return

    giros = verificar_usuario(user.id, user.username)
    apelido_user, _ = obter_apelido(user.id, user.username)

    if giros <= 0:
        sem_giros_txt = renderizar_template(obter_texto_publico("sem_giros"), apelido=esc(apelido_user))
        await msg.reply_text(sem_giros_txt, parse_mode="HTML")
        return

    qtd_preset = 0
    if context.args:
        arg0 = context.args[0].strip().lower()
        if arg0 in ("tudo", "all", "todos", "max"):
            qtd_preset = giros
        elif arg0.isdigit():
            val = int(arg0)
            if val < 1:
                await msg.reply_text("⚠️ Escolha pelo menos <b>1</b> giro.", parse_mode="HTML")
                return
            if val > giros:
                await msg.reply_text(
                    f"⚠️ Você só tem <b>{giros}</b> giros disponíveis no momento!",
                    parse_mode="HTML"
                )
                return
            qtd_preset = val
        else:
            await msg.reply_text(
                "Uso: <code>/girar</code>, <code>/girar 5</code> ou <code>/girar tudo</code>",
                parse_mode="HTML"
            )
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
        linha.append(InlineKeyboardButton(cat_nome, callback_data=f"rcat|{cat_id}|{qtd_preset}|{user.id}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("🌐 Mix Aleatório (Tudo)", callback_data=f"rcat|all|{qtd_preset}|{user.id}")])

    texto_menu = renderizar_template(
        obter_texto_publico("menu_girar"),
        apelido=esc(apelido_user),
        giros=giros,
    )
    if qtd_preset > 0:
        texto_menu += f"\n🎯 <i>Quantidade selecionada: <b>{qtd_preset}x</b></i>"

    markup = InlineKeyboardMarkup(botoes)

    try:
        await msg.reply_text(texto_menu, reply_markup=markup, parse_mode="HTML")
    except Exception:
        kwargs = {"chat_id": chat.id, "text": texto_menu, "reply_markup": markup, "parse_mode": "HTML"}
        thread_id = getattr(msg, "message_thread_id", None)
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        try:
            await context.bot.send_message(**kwargs)
        except Exception:
            await context.bot.send_message(
                chat_id=chat.id,
                text="⚠️ Não consegui exibir os botões neste grupo. Verifique se o bot tem permissão para enviar mensagens e usar teclados inline."
            )

def _montar_teclado_qtd_giros(cat_alvo: str, sub_alvo: str, subsub_alvo: str, giros: int, dono_id: int) -> InlineKeyboardMarkup:
    botoes = [
        [
            InlineKeyboardButton("🎲 1x", callback_data=f"rqtd|{cat_alvo}|{sub_alvo}|{subsub_alvo}|1|{dono_id}"),
            InlineKeyboardButton(f"🔥 Todos ({giros}x)", callback_data=f"rqtd|{cat_alvo}|{sub_alvo}|{subsub_alvo}|all|{dono_id}"),
        ]
    ]
    extras = []
    if giros > 3:
        extras.append(InlineKeyboardButton("🎲 3x", callback_data=f"rqtd|{cat_alvo}|{sub_alvo}|{subsub_alvo}|3|{dono_id}"))
    if giros > 5:
        extras.append(InlineKeyboardButton("🎲 5x", callback_data=f"rqtd|{cat_alvo}|{sub_alvo}|{subsub_alvo}|5|{dono_id}"))
    if giros > 2:
        extras.append(InlineKeyboardButton("✏️ Escolher qtd", callback_data=f"rqtd|{cat_alvo}|{sub_alvo}|{subsub_alvo}|custom|{dono_id}"))
    if extras:
        botoes.append(extras)
    return InlineKeyboardMarkup(botoes)

async def callback_girar_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, cat_alvo, qtd_str, dono_id_str = query.data.split("|")
    dono_id = int(dono_id_str)
    qtd_preset = int(qtd_str)

    if query.from_user.id != dono_id:
        await query.answer("Esse menu de giro não é seu!", show_alert=True)
        return

    giros = verificar_usuario(query.from_user.id, query.from_user.username)
    if giros <= 0:
        await query.answer("Seus giros acabaram!", show_alert=True)
        return

    if cat_alvo == "all":
        if qtd_preset > 0:
            await executar_sorteio(query, context, cat_id=None, sub_id=None, subsub_id=None, qtd_giros=min(qtd_preset, giros))
            return
        if giros == 1:
            await executar_sorteio(query, context, cat_id=None, sub_id=None, subsub_id=None, qtd_giros=1)
            return
        await query.answer()
        await query.edit_message_text(
            f"🌐 <b>Mix Aleatório (Tudo)</b>\n🍃 Você tem <b>{giros}</b> giros. Quantos deseja gastar?",
            reply_markup=_montar_teclado_qtd_giros("all", "all", "all", giros, dono_id),
            parse_mode="HTML"
        )
        return

    cat_id = int(cat_alvo)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM subcategorias WHERE categoria_id = %s", (cat_id,))
    total_subs = cur.fetchone()[0]
    cur.execute("SELECT id, nome FROM subcategorias WHERE categoria_id = %s ORDER BY RANDOM() LIMIT 5", (cat_id,))
    subs = cur.fetchall()
    cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
    cat_row = cur.fetchone()
    cur.close()
    conn.close()

    if not cat_row:
        await query.answer("Categoria não encontrada.", show_alert=True)
        return

    if not subs:
        if qtd_preset > 0:
            await executar_sorteio(query, context, cat_id=cat_id, sub_id=None, subsub_id=None, qtd_giros=min(qtd_preset, giros))
            return
        if giros == 1:
            await executar_sorteio(query, context, cat_id=cat_id, sub_id=None, subsub_id=None, qtd_giros=1)
            return
        await query.answer()
        await query.edit_message_text(
            f"💿 <b>{esc(cat_row[0])}</b>\n🍃 Você tem <b>{giros}</b> giros. Quantos deseja gastar?",
            reply_markup=_montar_teclado_qtd_giros(str(cat_id), "all", "all", giros, dono_id),
            parse_mode="HTML"
        )
        return

    botoes = []
    linha = []
    for sub_id, sub_nome in subs:
        linha.append(InlineKeyboardButton(sub_nome, callback_data=f"rsub|{cat_id}|{sub_id}|{qtd_preset}|{dono_id}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton(f"🌐 Todos de {cat_row[0]}", callback_data=f"rsub|{cat_id}|all|{qtd_preset}|{dono_id}")])
    if False and total_subs > 5:
        botoes.append([InlineKeyboardButton("🔀 Ver outras 5 opções", callback_data=f"rmore|sub|{cat_id}|{qtd_preset}|{dono_id}")])

    await query.answer()
    await query.edit_message_text(
        f"💿 <b>{esc(cat_row[0])}</b> — Escolha a subcategoria:",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )

async def callback_girar_subcategoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, cat_id_str, sub_alvo, qtd_str, dono_id_str = query.data.split("|")
    dono_id = int(dono_id_str)
    qtd_preset = int(qtd_str)

    if query.from_user.id != dono_id:
        await query.answer("Esse menu de giro não é seu!", show_alert=True)
        return

    giros = verificar_usuario(query.from_user.id, query.from_user.username)
    if giros <= 0:
        await query.answer("Seus giros acabaram!", show_alert=True)
        return

    cat_id = int(cat_id_str)

    if sub_alvo == "all":
        if qtd_preset > 0:
            await executar_sorteio(query, context, cat_id=cat_id, sub_id=None, subsub_id=None, qtd_giros=min(qtd_preset, giros))
            return
        if giros == 1:
            await executar_sorteio(query, context, cat_id=cat_id, sub_id=None, subsub_id=None, qtd_giros=1)
            return
        await query.answer()
        await query.edit_message_text(
            f"🍃 Você tem <b>{giros}</b> giros disponíveis. Quantos deseja gastar agora?",
            reply_markup=_montar_teclado_qtd_giros(cat_id_str, "all", "all", giros, dono_id),
            parse_mode="HTML"
        )
        return

    sub_id = int(sub_alvo)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
    r_cat = cur.fetchone()
    cur.execute("SELECT nome FROM subcategorias WHERE id = %s", (sub_id,))
    r_sub = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM subsubcategorias WHERE subcategoria_id = %s", (sub_id,))
    total_grupos = cur.fetchone()[0]
    cur.execute("SELECT id, nome FROM subsubcategorias WHERE subcategoria_id = %s ORDER BY RANDOM() LIMIT 5", (sub_id,))
    subsubs = cur.fetchall()
    cur.close()
    conn.close()

    if subsubs:
        cat_nome = r_cat[0] if r_cat else ""
        sub_nome = r_sub[0] if r_sub else ""
        botoes = []
        linha = []
        for subsub_id, subsub_nome in subsubs:
            linha.append(InlineKeyboardButton(subsub_nome, callback_data=f"rsubsub|{cat_id}|{sub_id}|{subsub_id}|{qtd_preset}|{dono_id}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)

        botoes.append([InlineKeyboardButton(f"🌐 Todos de {sub_nome}", callback_data=f"rsubsub|{cat_id}|{sub_id}|all|{qtd_preset}|{dono_id}")])
        if False and total_grupos > 5:
            botoes.append([InlineKeyboardButton("🔀 Ver outros 5 grupos", callback_data=f"rmore|group|{cat_id}|{sub_id}|{qtd_preset}|{dono_id}")])

        await query.answer()
        await query.edit_message_text(
            f"💿 <b>{esc(cat_nome)} • {esc(sub_nome)}</b> — Escolha o grupo / seção:",
            reply_markup=InlineKeyboardMarkup(botoes),
            parse_mode="HTML"
        )
        return

    if qtd_preset > 0:
        await executar_sorteio(query, context, cat_id=cat_id, sub_id=sub_id, subsub_id=None, qtd_giros=min(qtd_preset, giros))
        return

    if giros == 1:
        await executar_sorteio(query, context, cat_id=cat_id, sub_id=sub_id, subsub_id=None, qtd_giros=1)
        return

    await query.answer()
    await query.edit_message_text(
        f"🍃 Você tem <b>{giros}</b> giros disponíveis. Quantos deseja gastar agora?",
        reply_markup=_montar_teclado_qtd_giros(cat_id_str, sub_alvo, "all", giros, dono_id),
        parse_mode="HTML"
    )

async def callback_girar_subcategoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; _,cat_s,sub_s,qtd_s,owner_s=query.data.split("|")
    owner=int(owner_s); preset=int(qtd_s)
    if query.from_user.id!=owner:
        await query.answer("Esse menu de giro não é seu!",show_alert=True); return
    giros=verificar_usuario(owner,query.from_user.username)
    if giros<=0:
        await query.answer("Seus giros acabaram!",show_alert=True); return
    cat_id=None if cat_s=="all" else int(cat_s)
    sub_id=None if sub_s=="all" else int(sub_s)
    if preset>0:
        await executar_sorteio(query,context,cat_id=cat_id,sub_id=sub_id,subsub_id=None,qtd_giros=min(preset,giros)); return
    if giros==1:
        await executar_sorteio(query,context,cat_id=cat_id,sub_id=sub_id,subsub_id=None,qtd_giros=1); return
    await query.answer()
    await query.edit_message_text(
        f"Você tem <b>{giros}</b> giros disponíveis. Quantos deseja gastar em {('toda a categoria' if sub_id is None else 'toda a subcategoria')}?",
        reply_markup=_montar_teclado_qtd_giros(cat_s,sub_s,"all",giros,owner),parse_mode="HTML")

async def callback_girar_subsubcategoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, cat_id_str, sub_id_str, subsub_alvo, qtd_str, dono_id_str = query.data.split("|")
    dono_id = int(dono_id_str)
    qtd_preset = int(qtd_str)

    if query.from_user.id != dono_id:
        await query.answer("Esse menu de giro não é seu!", show_alert=True)
        return

    giros = verificar_usuario(query.from_user.id, query.from_user.username)
    if giros <= 0:
        await query.answer("Seus giros acabaram!", show_alert=True)
        return

    cat_id = int(cat_id_str)
    sub_id = int(sub_id_str)
    subsub_id = None if subsub_alvo == "all" else int(subsub_alvo)

    if qtd_preset > 0:
        await executar_sorteio(query, context, cat_id=cat_id, sub_id=sub_id, subsub_id=subsub_id, qtd_giros=min(qtd_preset, giros))
        return

    if giros == 1:
        await executar_sorteio(query, context, cat_id=cat_id, sub_id=sub_id, subsub_id=subsub_id, qtd_giros=1)
        return

    await query.answer()
    await query.edit_message_text(
        f"🍃 Você tem <b>{giros}</b> giros disponíveis. Quantos deseja gastar agora?",
        reply_markup=_montar_teclado_qtd_giros(cat_id_str, sub_id_str, subsub_alvo, giros, dono_id),
        parse_mode="HTML"
    )

async def callback_girar_mais_opcoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    partes = query.data.split("|")
    tipo = partes[1]
    if tipo == "sub":
        _, _, cat_str, qtd_str, dono_str = partes
        cat_id, qtd, dono_id = int(cat_str), int(qtd_str), int(dono_str)
        if query.from_user.id != dono_id:
            await query.answer("Esse menu de giro não é seu!", show_alert=True)
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
        cat = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM subcategorias WHERE categoria_id = %s", (cat_id,))
        total = cur.fetchone()[0]
        cur.execute("SELECT id, nome FROM subcategorias WHERE categoria_id = %s ORDER BY RANDOM() LIMIT 5", (cat_id,))
        itens = cur.fetchall()
        cur.close()
        conn.close()
        botoes = []
        linha = []
        for sid, nome in itens:
            linha.append(InlineKeyboardButton(nome, callback_data=f"rsub|{cat_id}|{sid}|{qtd}|{dono_id}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
        botoes.append([InlineKeyboardButton(f"🌐 Todos de {cat[0]}", callback_data=f"rsub|{cat_id}|all|{qtd}|{dono_id}")])
        if False and total > 5:
            botoes.append([InlineKeyboardButton("🔀 Ver outras 5 opções", callback_data=f"rmore|sub|{cat_id}|{qtd}|{dono_id}")])
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(botoes))
        return

    _, _, cat_str, sub_str, qtd_str, dono_str = partes
    cat_id, sub_id, qtd, dono_id = int(cat_str), int(sub_str), int(qtd_str), int(dono_str)
    if query.from_user.id != dono_id:
        await query.answer("Esse menu de giro não é seu!", show_alert=True)
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM subcategorias WHERE id = %s", (sub_id,))
    sub = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM subsubcategorias WHERE subcategoria_id = %s", (sub_id,))
    total = cur.fetchone()[0]
    cur.execute("SELECT id, nome FROM subsubcategorias WHERE subcategoria_id = %s ORDER BY RANDOM() LIMIT 5", (sub_id,))
    itens = cur.fetchall()
    cur.close()
    conn.close()
    botoes = []
    linha = []
    for ssid, nome in itens:
        linha.append(InlineKeyboardButton(nome, callback_data=f"rsubsub|{cat_id}|{sub_id}|{ssid}|{qtd}|{dono_id}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    botoes.append([InlineKeyboardButton(f"🌐 Todos de {sub[0]}", callback_data=f"rsubsub|{cat_id}|{sub_id}|all|{qtd}|{dono_id}")])
    if False and total > 5:
        botoes.append([InlineKeyboardButton("🔀 Ver outros 5 grupos", callback_data=f"rmore|group|{cat_id}|{sub_id}|{qtd}|{dono_id}")])
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(botoes))

async def callback_girar_quantidade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, cat_alvo, sub_alvo, subsub_alvo, qtd_modo, dono_id_str = query.data.split("|")
    dono_id = int(dono_id_str)

    if query.from_user.id != dono_id:
        await query.answer("Esse menu de giro não é seu!", show_alert=True)
        return

    giros = verificar_usuario(query.from_user.id, query.from_user.username)
    if giros <= 0:
        await query.answer("Seus giros acabaram!", show_alert=True)
        return

    cat_id = None if cat_alvo == "all" else int(cat_alvo)
    sub_id = None if sub_alvo == "all" else int(sub_alvo)
    subsub_id = None if subsub_alvo == "all" else int(subsub_alvo)

    if qtd_modo == "custom":
        await query.answer()
        chat_id = query.message.chat_id
        pending = context.bot_data.setdefault("custom_spin", {})
        pending[(chat_id, dono_id)] = {
            "cat_id": cat_id,
            "sub_id": sub_id,
            "subsub_id": subsub_id,
            "menu_msg": query.message,
        }
        await query.edit_message_text(
            f"✏️ Digite no chat <b>quantos giros</b> deseja gastar (de <b>1</b> a <b>{giros}</b>):",
            parse_mode="HTML"
        )
        return

    qtd_giros = giros if qtd_modo == "all" else min(int(qtd_modo), giros)
    await executar_sorteio(query, context, cat_id=cat_id, sub_id=sub_id, subsub_id=subsub_id, qtd_giros=qtd_giros)

async def handler_custom_spin_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recebe o número digitado quando o jogador escolhe 'Escolher qtd' no /girar."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or not msg.text:
        return

    pending = context.bot_data.get("custom_spin", {})
    chave = (chat.id, user.id)
    if chave not in pending:
        return

    txt = msg.text.strip().lower()
    giros = verificar_usuario(user.id, user.username)

    if txt in ("cancelar", "sair"):
        pending.pop(chave, None)
        await msg.reply_text("❌ Seleção de giros cancelada.")
        return

    if txt in ("tudo", "all", "todos", "max"):
        qtd = giros
    elif txt.isdigit():
        qtd = int(txt)
    else:
        await msg.reply_text(f"⚠️ Digite apenas um número de <b>1</b> a <b>{giros}</b> (ou <code>cancelar</code>).", parse_mode="HTML")
        return

    if qtd < 1 or qtd > giros:
        await msg.reply_text(
            f"⚠️ Quantidade inválida! Seu limite atual é de <b>{giros}</b> giros. Digite um número entre <b>1</b> e <b>{giros}</b>:",
            parse_mode="HTML"
        )
        return

    dados = pending.pop(chave)
    await _processar_giros_core(
        user=user,
        chat_id=chat.id,
        thread_id=getattr(msg, "message_thread_id", None),
        reply_anchor=msg,
        menu_message=dados.get("menu_msg"),
        bot=context.bot,
        cat_id=dados["cat_id"],
        sub_id=dados["sub_id"],
        subsub_id=dados.get("subsub_id"),
        qtd_giros=qtd,
        query=None,
    )

async def executar_sorteio(query, context: ContextTypes.DEFAULT_TYPE, cat_id=None, sub_id=None, subsub_id=None, qtd_giros: int = 1, event_id=None):
    await _processar_giros_core(
        user=query.from_user,
        chat_id=query.message.chat_id,
        thread_id=getattr(query.message, "message_thread_id", None),
        reply_anchor=query.message,
        menu_message=query.message,
        bot=context.bot,
        cat_id=cat_id,
        sub_id=sub_id,
        subsub_id=subsub_id,
        qtd_giros=qtd_giros,
        event_id=event_id,
        query=query,
    )

async def _processar_giros_core(
    user,
    chat_id: int,
    thread_id,
    reply_anchor,
    menu_message,
    bot,
    cat_id=None,
    sub_id=None,
    subsub_id=None,
    qtd_giros: int = 1,
    event_id=None,
    query=None,
):
    if user.id in GIROS_EM_ANDAMENTO:
        if query:
            await query.answer("Aguarde o giro atual terminar!", show_alert=True)
        return

    GIROS_EM_ANDAMENTO.add(user.id)
    try:
        giros = verificar_usuario(user.id, user.username)
        apelido_user, _ = obter_apelido(user.id, user.username)

        if giros <= 0:
            sem_giros_txt = renderizar_template(obter_texto_publico("sem_giros"), apelido=esc(apelido_user))
            if query:
                await query.answer("Seus giros acabaram!", show_alert=True)
            if menu_message:
                try:
                    await menu_message.edit_text(sem_giros_txt, parse_mode="HTML")
                except Exception:
                    pass
            return

        qtd_efetiva = max(1, min(qtd_giros, giros))

        conn = get_db()
        cur = conn.cursor()

        sql_base = """
            SELECT c.id, c.nome, cat.id, cat.nome, sub.id, sub.nome, subsub.id, subsub.nome,
                   c.tier, c.peso_drop, c.file_id, c.media_type
            FROM cartas c
            JOIN categorias cat ON c.categoria_id = cat.id
            LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
            LEFT JOIN subsubcategorias subsub ON c.subsubcategoria_id = subsub.id
        """
        if cat_id is None:
            cur.execute(sql_base)
        elif sub_id is None:
            cur.execute(sql_base + " WHERE c.categoria_id = %s", (cat_id,))
        elif subsub_id is None:
            cur.execute(sql_base + " WHERE c.categoria_id = %s AND c.subcategoria_id = %s", (cat_id, sub_id))
        else:
            cur.execute(
                sql_base + " WHERE c.categoria_id = %s AND c.subcategoria_id = %s AND c.subsubcategoria_id = %s",
                (cat_id, sub_id, subsub_id)
            )

        if event_id is not None:
            if cat_id is None:
                cur.execute(sql_base + " WHERE c.event_id = %s", (event_id,))
            elif sub_id is None:
                cur.execute(sql_base + " WHERE c.event_id = %s AND c.categoria_id = %s", (event_id, cat_id))
            elif subsub_id is None:
                cur.execute(sql_base + " WHERE c.event_id = %s AND c.categoria_id = %s AND c.subcategoria_id = %s", (event_id, cat_id, sub_id))
            else:
                cur.execute(sql_base + " WHERE c.event_id = %s AND c.categoria_id = %s AND c.subcategoria_id = %s AND c.subsubcategoria_id = %s", (event_id, cat_id, sub_id, subsub_id))

        cartas = cur.fetchall()
        if not cartas:
            if query:
                await query.answer("Ainda não há cartas cadastradas aqui.", show_alert=True)
            else:
                await reply_anchor.reply_text("Ainda não há cartas cadastradas nessa categoria.")
            cur.close()
            conn.close()
            return

        if query:
            await query.answer()

        cartas_ss_plus = [c for c in cartas if c[8] == 6]
        cartas_normais = [c for c in cartas if c[8] != 6]
        if cartas_normais:
            sorteadas = []
            for _ in range(qtd_efetiva):
                if cartas_ss_plus and random.random() < CHANCE_SS_PLUS:
                    sorteadas.append(random.choice(cartas_ss_plus))
                else:
                    sorteadas.append(random.choices(cartas_normais, weights=[c[9] for c in cartas_normais], k=1)[0])
        else:
            sorteadas = random.choices(cartas_ss_plus or cartas, weights=[c[9] for c in (cartas_ss_plus or cartas)], k=qtd_efetiva)

        cur.execute("UPDATE usuarios SET giros_restantes = giros_restantes - %s WHERE user_id = %s", (qtd_efetiva, user.id))

        # Agrupa as cartas sorteadas para atualizar o inventário em poucas queries
        mapa_cartas = {c[0]: c for c in cartas}
        contagem_sorteio = Counter(c[0] for c in sorteadas)
        tags_por_carta = _tags_por_carta(list(contagem_sorteio.keys()))

        cur.execute(
            "SELECT id, carta_id, quantidade FROM inventario WHERE user_id = %s AND carta_id = ANY(%s)",
            (user.id, list(contagem_sorteio.keys()))
        )
        inv_atual = {cid: (inv_id, qtd) for inv_id, cid, qtd in cur.fetchall()}

        status_por_carta = {}
        for cid, ganhas in contagem_sorteio.items():
            if cid in inv_atual:
                inv_id, qtd_antiga = inv_atual[cid]
                nova_qtd = qtd_antiga + ganhas
                cur.execute("UPDATE inventario SET quantidade = %s WHERE id = %s", (nova_qtd, inv_id))
                status_por_carta[cid] = {"qtd_total": nova_qtd, "eh_nova": False}
            else:
                cur.execute(
                    "INSERT INTO inventario (user_id, carta_id, quantidade) VALUES (%s, %s, %s)",
                    (user.id, cid, ganhas)
                )
                status_por_carta[cid] = {"qtd_total": ganhas, "eh_nova": True}

        giros_restantes = giros - qtd_efetiva
        rodape_txt = renderizar_template(
            obter_texto_publico("giro_rodape"),
            apelido=esc(apelido_user),
            qtd_giros=qtd_efetiva,
            giros_restantes=giros_restantes,
        )

        if qtd_efetiva == 1:
            c_tuple = sorteadas[0]
            (
                carta_id, nome_carta, cid_cat, nome_cat, cid_sub, nome_sub,
                cid_subsub, nome_subsub, tier, _, file_id, media_type
            ) = c_tuple
            st = status_por_carta[carta_id]

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

            tier_info = TIERS_CONFIG[tier]
            tier_emoji = obter_emoji_tier(tier)
            cat_codigo, local_str = formatar_local_carta(
                cid_cat, nome_cat, cid_sub, nome_sub, cid_subsub, nome_subsub
            )

            legenda = renderizar_template(
                obter_texto_publico("giro_resultado"),
                apelido=esc(apelido_user),
                carta_id=carta_id,
                nome=esc(nome_carta),
                tier_emoji=tier_emoji,
                tier_curto=tier_info["curto"],
                tier_nome=tier_info["nome"],
                cat_codigo=cat_codigo,
                local=local_str,
                qtd=st["qtd_total"],
                tiers_obtidos=tiers_obtidos,
                tiers_cadastrados=tiers_cadastrados,
                giros_restantes=giros_restantes,
            )
            if tier == 6:
                legenda += "\n\n💠 <b>SS+ ultra raro!</b> Você pode vender esta carta na loja por <b>1.000 EccoCoins</b>."

            tags_text=_texto_tags([carta_id],tags_por_carta)
            if tags_text: legenda += "\n"+tags_text
            await enviar_midia_carta(reply_anchor, file_id, media_type, legenda, bot=bot)
            await enviar_texto_seguro(chat_id, rodape_txt, bot, thread_id=thread_id)
        else:
            conn.commit()
            cur.close()
            conn.close()

            destaque_tuple = max(sorteadas, key=lambda x: x[8])
            d_id, d_nome, _, _, _, _, _, _, d_tier, _, d_file, d_media = destaque_tuple
            destaque_emoji = obter_emoji_tier(d_tier)
            destaque_info = TIERS_CONFIG[d_tier]

            cabecalho = renderizar_template(
                obter_texto_publico("giro_multi"),
                apelido=esc(apelido_user),
                qtd_giros=qtd_efetiva,
                giros_restantes=giros_restantes,
            )

            linhas = [
                cabecalho,
                f"\n🌟 <b>Destaque:</b> {destaque_emoji} <b>#{d_id} {esc(d_nome)}</b> <i>[{destaque_info['curto']}]</i>\n",
            ]

            for cid, vezes in contagem_sorteio.items():
                _, c_nome, _, _, _, _, _, _, c_tier, _, _, _ = mapa_cartas[cid]
                st = status_por_carta[cid]
                em = obter_emoji_tier(c_tier)
                t_c = TIERS_CONFIG[c_tier]["curto"]
                tag_nova = " 🆕" if st["eh_nova"] else f" (total: {st['qtd_total']}x)"
                vezes_txt = f" <b>+{vezes}x</b>" if vezes > 1 else ""
                linhas.append(renderizar_template(
                    obter_texto_publico("giro_multi_item"),
                    tier_emoji=em,
                    carta_id=cid,
                    nome=esc(c_nome),
                    tier_curto=t_c,
                    vezes_txt=vezes_txt,
                    tag_nova=tag_nova,
                ))
                tags_text=_texto_tags([cid],tags_por_carta)
                if tags_text: linhas.append(tags_text)
                if c_tier == 6:
                    linhas.append("💠 <b>SS+ ultra raro!</b> Cada carta SS+ pode ser vendida por <b>1.000 EccoCoins</b> na loja.")

            legenda = "\n".join(linhas)
            legenda_resultado = legenda
            if len(legenda_resultado) > 1000:
                compacto = linhas[:2]
                usados = sum(len(item) + 1 for item in compacto)
                omitidos = 0
                itens = linhas[2:]
                for indice, item in enumerate(itens):
                    if usados + len(item) + 1 > 940:
                        omitidos = len(itens) - indice
                        break
                    compacto.append(item)
                    usados += len(item) + 1
                if omitidos:
                    compacto.append(f"<i>… e mais {omitidos} resultado(s)</i>")
                legenda_resultado = "\n".join(compacto)
            await enviar_midia_carta(reply_anchor, d_file, d_media, legenda_resultado, bot=bot)
            await enviar_texto_seguro(chat_id, rodape_txt, bot, thread_id=thread_id)

        if menu_message:
            try:
                await menu_message.delete()
            except Exception:
                pass
    finally:
        GIROS_EM_ANDAMENTO.discard(user.id)

# ==========================================
# RANKING DE USUÁRIOS POR ARTISTA / PERSONAGEM
# ==========================================
def _montar_texto_ranking_artista(nome_oficial: str, caller_id: int) -> str:
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT cat.nome, sub.nome, subsub.nome, COUNT(*)
        FROM cartas c
        JOIN categorias cat ON c.categoria_id = cat.id
        LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
        LEFT JOIN subsubcategorias subsub ON c.subsubcategoria_id = subsub.id
        WHERE LOWER(c.nome) = LOWER(%s)
        GROUP BY cat.nome, sub.nome, subsub.nome
        LIMIT 1
    """, (nome_oficial,))
    meta = cur.fetchone()
    cat_nome, sub_nome, subsub_nome, total_tiers_artista = meta if meta else ("", None, None, 5)

    cur.execute("""
        SELECT
            u.user_id,
            u.username,
            u.apelido,
            COUNT(DISTINCT c.tier) AS tiers_unicos,
            MAX(c.tier) AS maior_tier,
            SUM(i.quantidade) AS total_cartas
        FROM inventario i
        JOIN cartas c ON i.carta_id = c.id
        JOIN usuarios u ON i.user_id = u.user_id
        WHERE LOWER(c.nome) = LOWER(%s) AND i.quantidade > 0
        GROUP BY u.user_id, u.username, u.apelido
        ORDER BY tiers_unicos DESC, maior_tier DESC, total_cartas DESC, u.user_id ASC
    """, (nome_oficial,))
    ranking = cur.fetchall()
    cur.close()
    conn.close()

    _, local_str = formatar_local_carta(0, cat_nome, None, sub_nome, None, subsub_nome)
    linhas = [
        f"🏆 <b>Ranking — {esc(nome_oficial)}</b> <i>({local_str})</i>\n"
    ]

    if not ranking:
        linhas.append("<i>Nenhum jogador desbloqueou cartas desse artista/personagem ainda.</i>")
        return "\n".join(linhas)

    medalhas = {1: "🥇", 2: "🥈", 3: "🥉"}
    pos_caller = None

    for idx, (uid, uname, apelido_db, tiers_unicos, maior_tier, total_cartas) in enumerate(ranking, start=1):
        if uid == caller_id:
            pos_caller = (idx, tiers_unicos, total_cartas)
        if idx > 10:
            continue

        if apelido_db and apelido_db.strip():
            nome_exib = apelido_db.strip()
        elif uname:
            nome_exib = f"@{uname}"
        else:
            nome_exib = f"Mixer #{str(uid)[-4:]}"

        prefixo = medalhas.get(idx, f"<b>{idx}.</b>")
        coroa = " 💠" if (tiers_unicos == total_tiers_artista and total_tiers_artista >= 5) else ""
        linhas.append(
            f"{prefixo} <b>{esc(nome_exib)}</b> — <b>{tiers_unicos}/{total_tiers_artista}</b> tiers "
            f"<i>({total_cartas} {'carta' if total_cartas == 1 else 'cartas'})</i>{coroa}"
        )

    if pos_caller and pos_caller[0] > 10:
        linhas.append(
            f"\n📍 <b>Sua posição:</b> #{pos_caller[0]} — {pos_caller[1]}/{total_tiers_artista} tiers ({pos_caller[2]} cartas)"
        )

    return "\n".join(linhas)

async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    verificar_usuario(user.id, user.username)
    busca = " ".join(context.args).strip()

    if not busca:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT MIN(id), MIN(nome) FROM cartas GROUP BY LOWER(nome) ORDER BY RANDOM() LIMIT 6")
        sugestoes = cur.fetchall()
        cur.close()
        conn.close()

        botoes = []
        linha = []
        for cid, anome in sugestoes:
            linha.append(InlineKeyboardButton(f"🏆 {anome}", callback_data=f"rank_cid|{cid}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)

        await msg.reply_text(
            "🏆 <b>Ranking por Artista / Personagem</b>\n\n"
            "Uso: <code>/ranking nome</code> (ex: <code>/ranking kuru</code>)\n"
            "Ou toque em uma sugestão abaixo:",
            reply_markup=InlineKeyboardMarkup(botoes) if botoes else None,
            parse_mode="HTML"
        )
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM cartas WHERE LOWER(nome) = LOWER(%s) LIMIT 1", (busca,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT nome FROM cartas WHERE LOWER(nome) LIKE LOWER(%s) ORDER BY nome ASC LIMIT 1", (f"%{busca}%",))
        row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        await msg.reply_text("Nenhum artista/personagem encontrado com esse nome.")
        return

    texto = _montar_texto_ranking_artista(row[0], user.id)
    await msg.reply_text(texto, parse_mode="HTML")

async def callback_ranking_cid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cid = int(query.data.split("|")[1])

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM cartas WHERE id = %s", (cid,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        await query.answer("Artista não encontrado.", show_alert=True)
        return

    await query.answer()
    texto = _montar_texto_ranking_artista(row[0], query.from_user.id)
    await query.message.reply_text(texto, parse_mode="HTML")

# ==========================================
# CATÁLOGO EM TEXTO COM BOTÕES E PAGINAÇÃO (/catalogo — 3 NÍVEIS)
# ==========================================
async def cmd_catalogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    verificar_usuario(user.id, user.username)

    if update.callback_query:
        await update.callback_query.answer()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM categorias ORDER BY id ASC")
    categorias = cur.fetchall()
    cur.close()
    conn.close()

    botoes = []
    linha = []
    for cid, cnome in categorias:
        linha.append(InlineKeyboardButton(cnome, callback_data=f"catlist|cat|{cid}|{user.id}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("🌐 Todas as Cartas", callback_data=f"catlist|view|all|all|all|0|{user.id}")])
    botoes.append([InlineKeyboardButton("❌ Fechar", callback_data=f"catlist|close|{user.id}")])

    texto = "📜 <b>Catálogo de Cartas</b>\n\nSelecione uma <b>categoria</b> para ver a lista:"
    botoes.append([InlineKeyboardButton("🔍 Pesquisar carta por nome", callback_data=f"catlist|search|{user.id}")])
    markup = InlineKeyboardMarkup(botoes)

    if update.callback_query:
        await update.callback_query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(texto, reply_markup=markup, parse_mode="HTML")

async def _mostrar_busca_catalogo(target, context, busca: str, page: int = 0):
    page_size=10; conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cartas WHERE nome ILIKE %s",(f"%{busca}%",)); total=cur.fetchone()[0]
    pages=max(1,(total+page_size-1)//page_size); page=max(0,min(page,pages-1))
    cur.execute("SELECT id,nome,tier FROM cartas WHERE nome ILIKE %s ORDER BY nome,tier,id LIMIT %s OFFSET %s",(f"%{busca}%",page_size,page*page_size)); rows=cur.fetchall(); cur.close(); conn.close()
    context.user_data["catalogo_search_query"]=busca
    uid=target.from_user.id if hasattr(target,"from_user") else context.user_data.get("catalogo_search_owner")
    buttons=[[InlineKeyboardButton(f"#{cid} {nome[:30]} · T{tier}",callback_data=f"catlist|carta|{cid}|{uid}")] for cid,nome,tier in rows]
    nav=[]
    if page: nav.append(InlineKeyboardButton("⬅️",callback_data=f"catlist|searchpage|{page-1}|{uid}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data=f"catlist|noop|{uid}"))
    if page+1<pages: nav.append(InlineKeyboardButton("➡️",callback_data=f"catlist|searchpage|{page+1}|{uid}"))
    if len(nav)>1: buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔍 Nova busca",callback_data=f"catlist|search|{uid}"),InlineKeyboardButton("⬅️ Catálogo",callback_data=f"catlist|home|{uid}")])
    text=f"Resultados no catálogo para <b>{esc(busca)}</b> · {total} carta(s):" if rows else f"Nenhuma carta encontrada para <b>{esc(busca)}</b>."
    if hasattr(target,"edit_message_text"):
        await target.answer(); await target.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")
    else:
        await target.reply_text(text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")

async def callback_catalogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    partes = query.data.split("|")
    acao = partes[1]
    dono_id = int(partes[-1])

    if query.from_user.id != dono_id:
        await query.answer("Esse menu pertence a outro jogador! Use /catalogo para abrir o seu.", show_alert=True)
        return

    if acao == "noop":
        await query.answer(); return

    if acao == "search":
        context.user_data["catalogo_search_pending"] = dono_id
        context.user_data["catalogo_search_owner"] = dono_id
        await query.answer(); await query.message.reply_text("Digite o nome da carta que deseja encontrar no catálogo:")
        return
    if acao == "searchpage":
        await _mostrar_busca_catalogo(query, context, context.user_data.get("catalogo_search_query", ""), int(partes[2])); return
    if acao == "carta":
        await query.answer(); await _enviar_carta_por_id(query, dono_id, int(partes[2]), context.bot); return

    if acao == "close":
        await query.answer("Fechado.")
        await query.edit_message_text("📜 Catálogo fechado.")
        return

    if acao == "home":
        await cmd_catalogo(update, context)
        return

    if acao == "cat":
        cat_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
        cat_row = cur.fetchone()
        cur.execute("SELECT id, nome FROM subcategorias WHERE categoria_id = %s ORDER BY id ASC", (cat_id,))
        subs = cur.fetchall()
        cur.close()
        conn.close()

        if not cat_row:
            await query.answer("Categoria não encontrada.", show_alert=True)
            return

        if not subs:
            await _renderizar_lista_catalogo(query, cat_alvo=str(cat_id), sub_alvo="all", subsub_alvo="all", page=0, user_id=dono_id)
            return

        await query.answer()
        botoes = []
        linha = []
        for sid, snome in subs:
            linha.append(InlineKeyboardButton(snome, callback_data=f"catlist|sub|{cat_id}|{sid}|{dono_id}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)

        botoes.append([InlineKeyboardButton(f"🌐 Todas de {cat_row[0]}", callback_data=f"catlist|view|{cat_id}|all|all|0|{dono_id}")])
        botoes.append([
            InlineKeyboardButton("⬅️ Categorias", callback_data=f"catlist|home|{dono_id}"),
            InlineKeyboardButton("❌ Fechar", callback_data=f"catlist|close|{dono_id}"),
        ])

        await query.edit_message_text(
            f"💿 <b>{esc(cat_row[0])}</b>\n\nEscolha a <b>subcategoria</b> para listar as cartas:",
            reply_markup=InlineKeyboardMarkup(botoes),
            parse_mode="HTML"
        )
        return

    if acao == "sub":
        cat_id = int(partes[2])
        sub_id = int(partes[3])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
        cat_row = cur.fetchone()
        cur.execute("SELECT nome FROM subcategorias WHERE id = %s", (sub_id,))
        sub_row = cur.fetchone()
        cur.execute("SELECT id, nome FROM subsubcategorias WHERE subcategoria_id = %s ORDER BY id ASC", (sub_id,))
        subsubs = cur.fetchall()
        cur.close()
        conn.close()

        if not cat_row or not sub_row:
            await query.answer("Subcategoria não encontrada.", show_alert=True)
            return

        if not subsubs:
            await _renderizar_lista_catalogo(query, cat_alvo=str(cat_id), sub_alvo=str(sub_id), subsub_alvo="all", page=0, user_id=dono_id)
            return

        await query.answer()
        botoes = []
        linha = []
        for ssid, ssnome in subsubs:
            linha.append(InlineKeyboardButton(ssnome, callback_data=f"catlist|view|{cat_id}|{sub_id}|{ssid}|0|{dono_id}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)

        botoes.append([InlineKeyboardButton(f"🌐 Todas de {sub_row[0]}", callback_data=f"catlist|view|{cat_id}|{sub_id}|all|0|{dono_id}")])
        botoes.append([
            InlineKeyboardButton("⬅️ Subcategorias", callback_data=f"catlist|cat|{cat_id}|{dono_id}"),
            InlineKeyboardButton("❌ Fechar", callback_data=f"catlist|close|{dono_id}"),
        ])

        await query.edit_message_text(
            f"💿 <b>{esc(cat_row[0])} • {esc(sub_row[0])}</b>\n\nEscolha o <b>grupo / seção</b> para listar as cartas:",
            reply_markup=InlineKeyboardMarkup(botoes),
            parse_mode="HTML"
        )
        return

    if acao == "view":
        cat_alvo = partes[2]
        sub_alvo = partes[3]
        subsub_alvo = partes[4]
        page = int(partes[5])
        await _renderizar_lista_catalogo(query, cat_alvo=cat_alvo, sub_alvo=sub_alvo, subsub_alvo=subsub_alvo, page=page, user_id=dono_id)

async def _renderizar_lista_catalogo(query, cat_alvo: str, sub_alvo: str, subsub_alvo: str, page: int, user_id: int):
    conn = get_db()
    cur = conn.cursor()

    titulo = "Todas as Cartas"
    where_sql = ""
    params: list = []
    tem_subsubs_na_sub = False

    if cat_alvo != "all":
        cat_id = int(cat_alvo)
        cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
        r_cat = cur.fetchone()
        cat_nome = r_cat[0] if r_cat else f"Cat #{cat_id}"

        if sub_alvo == "all":
            titulo = esc(cat_nome)
            where_sql = "WHERE c.categoria_id = %s"
            params = [cat_id]
        else:
            sub_id = int(sub_alvo)
            cur.execute("SELECT nome FROM subcategorias WHERE id = %s", (sub_id,))
            r_sub = cur.fetchone()
            sub_nome = r_sub[0] if r_sub else f"Sub #{sub_id}"
            cur.execute("SELECT COUNT(*) FROM subsubcategorias WHERE subcategoria_id = %s", (sub_id,))
            tem_subsubs_na_sub = cur.fetchone()[0] > 0

            if subsub_alvo == "all":
                titulo = f"{esc(cat_nome)} • {esc(sub_nome)}"
                where_sql = "WHERE c.categoria_id = %s AND c.subcategoria_id = %s"
                params = [cat_id, sub_id]
            else:
                subsub_id = int(subsub_alvo)
                cur.execute("SELECT nome FROM subsubcategorias WHERE id = %s", (subsub_id,))
                r_subsub = cur.fetchone()
                subsub_nome = r_subsub[0] if r_subsub else f"Grupo #{subsub_id}"
                titulo = f"{esc(cat_nome)} • {esc(sub_nome)} • {esc(subsub_nome)}"
                where_sql = "WHERE c.categoria_id = %s AND c.subcategoria_id = %s AND c.subsubcategoria_id = %s"
                params = [cat_id, sub_id, subsub_id]

    cur.execute(f"SELECT COUNT(*) FROM cartas c {where_sql}", tuple(params))
    total = cur.fetchone()[0]

    if total == 0:
        cur.close()
        conn.close()
        await query.answer("Nenhuma carta cadastrada nessa seção.", show_alert=True)
        return

    total_paginas = max(1, (total + CAT_PAGE_SIZE - 1) // CAT_PAGE_SIZE)
    page = max(0, min(page, total_paginas - 1))
    offset = page * CAT_PAGE_SIZE

    cur.execute(f"""
        SELECT c.id, c.nome, c.tier, cat.nome, sub.nome, subsub.nome
        FROM cartas c
        JOIN categorias cat ON c.categoria_id = cat.id
        LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
        LEFT JOIN subsubcategorias subsub ON c.subsubcategoria_id = subsub.id
        {where_sql}
        ORDER BY c.nome ASC, c.tier ASC, c.id ASC
        LIMIT %s OFFSET %s
    """, tuple(params + [CAT_PAGE_SIZE, offset]))
    rows = cur.fetchall()

    cur.execute("SELECT carta_id FROM inventario WHERE user_id = %s AND quantidade > 0", (user_id,))
    possuidas = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.close()
    tags_por_carta = _tags_por_carta([r[0] for r in rows])

    await query.answer()

    linhas = [
        f"📜 <b>Catálogo — {titulo}</b>",
        f"📦 <b>{total}</b> cartas | Página <b>{page + 1}/{total_paginas}</b>\n",
    ]
    for cid, nome, tier, c_nome, s_nome, ss_nome in rows:
        t_cfg = TIERS_CONFIG[tier]
        t_em = obter_emoji_tier(tier)
        marca = " ✅" if cid in possuidas else ""
        extra_local = ""
        if cat_alvo == "all" or (subsub_alvo == "all" and ss_nome):
            _, loc = formatar_local_carta(0, c_nome, None, s_nome, None, ss_nome)
            extra_local = f" <i>({loc})</i>"
        linhas.append(f"{t_em} <code>#{cid}</code> <b>{esc(nome)}</b> — <i>{t_cfg['curto']}</i>{extra_local}{marca}")

    botoes = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"catlist|view|{cat_alvo}|{sub_alvo}|{subsub_alvo}|{page - 1}|{user_id}"))
    if page + 1 < total_paginas:
        nav.append(InlineKeyboardButton("Próxima ➡️", callback_data=f"catlist|view|{cat_alvo}|{sub_alvo}|{subsub_alvo}|{page + 1}|{user_id}"))
    if nav:
        botoes.append(nav)

    for cid,nome,tier,c_nome,s_nome,ss_nome in rows:
        tags_text=_texto_tags([cid],tags_por_carta)
        if tags_text: linhas.append(f"  {tags_text}")

    rodape = []
    if sub_alvo != "all" and tem_subsubs_na_sub:
        rodape.append(InlineKeyboardButton("🏷️ Grupos", callback_data=f"catlist|sub|{cat_alvo}|{sub_alvo}|{user_id}"))
    if cat_alvo != "all":
        rodape.append(InlineKeyboardButton("📂 Subcategorias", callback_data=f"catlist|cat|{cat_alvo}|{user_id}"))
    rodape.append(InlineKeyboardButton("🔙 Categorias", callback_data=f"catlist|home|{user_id}"))
    rodape.append(InlineKeyboardButton("❌ Fechar", callback_data=f"catlist|close|{user_id}"))
    botoes.append(rodape)

    await query.edit_message_text(
        "\n".join(linhas),
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )

async def cmd_artista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    verificar_usuario(user.id, user.username)

    busca = " ".join(context.args).strip()
    if not busca:
        await msg.reply_text("Uso: <code>/artista nome</code> (ex: <code>/artista kuru</code>)", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT c.id, c.nome, cat.nome, sub.nome, subsub.nome, c.tier
        FROM cartas c
        JOIN categorias cat ON c.categoria_id = cat.id
        LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
        LEFT JOIN subsubcategorias subsub ON c.subsubcategoria_id = subsub.id
        WHERE LOWER(c.nome) = LOWER(%s)
        ORDER BY c.tier ASC
    """, (busca,))
    cartas = cur.fetchall()

    if not cartas:
        cur.execute("""
            SELECT c.id, c.nome, cat.nome, sub.nome, subsub.nome, c.tier
            FROM cartas c
            JOIN categorias cat ON c.categoria_id = cat.id
            LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
            LEFT JOIN subsubcategorias subsub ON c.subsubcategoria_id = subsub.id
            WHERE LOWER(c.nome) LIKE LOWER(%s)
            ORDER BY c.nome ASC, c.tier ASC
        """, (f"%{busca}%",))
        resultados = cur.fetchall()
        if not resultados:
            await msg.reply_text("Nenhum artista/personagem encontrado com esse nome.")
            cur.close()
            conn.close()
            return
        nome_alvo = resultados[0][1].lower()
        cartas = [r for r in resultados if r[1].lower() == nome_alvo]

    nome_oficial = cartas[0][1]
    primeiro_cid = cartas[0][0]
    cat_nome = cartas[0][2]
    sub_nome = cartas[0][3]
    subsub_nome = cartas[0][4]
    mapa_tiers = {c[5]: c[0] for c in cartas}

    cur.execute("""
        SELECT c.tier, i.quantidade
        FROM inventario i
        JOIN cartas c ON i.carta_id = c.id
        WHERE i.user_id = %s AND LOWER(c.nome) = LOWER(%s)
    """, (user.id, nome_oficial))
    posse = dict(cur.fetchall())
    cur.close()
    conn.close()

    _, local_str = formatar_local_carta(0, cat_nome, None, sub_nome, None, subsub_nome)
    obtidas = len(posse)
    total = len(mapa_tiers)

    linhas = [
        f"🎧 <b>{esc(nome_oficial)}</b> <i>({local_str})</i>",
        f"💿 <b>Progresso:</b> {obtidas}/{total} cartas\n"
    ]

    for t in range(1, 7):
        t_cfg = TIERS_CONFIG[t]
        t_em = obter_emoji_tier(t)
        if t in mapa_tiers:
            cid = mapa_tiers[t]
            if t in posse:
                linhas.append(f"{t_em} <b>{t_cfg['nome']}:</b> ✅ (<code>#{cid}</code> — {posse[t]}x)")
            else:
                linhas.append(f"{t_em} <b>{t_cfg['nome']}:</b> ❌ (<code>#{cid}</code>)")
        else:
            linhas.append(f"{t_em} <b>{t_cfg['nome']}:</b> ➖ <i>(Não cadastrada)</i>")

    tags_por_carta=_tags_por_carta(list(mapa_tiers.values()))
    for cid in mapa_tiers.values():
        tags_text=_texto_tags([cid],tags_por_carta)
        if tags_text: linhas.append(f"  #{cid} · {tags_text}")
    botoes = [[InlineKeyboardButton("🏆 Ver Ranking deste Artista", callback_data=f"rank_cid|{primeiro_cid}")]]
    await msg.reply_text("\n".join(linhas), reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")

async def cmd_colecao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    verificar_usuario(user.id, user.username)
    tag_name = " ".join(context.args).strip() if context.args else None
    await _mostrar_colecao(msg, user.id, user.username, 0, tag_name=tag_name, callback_prefix="col")
    return
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
        await msg.reply_text("Sua coleção está vazia. Use /girar para pegar sua primeira carta!")
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
        selo = f"{obter_emoji_tier(5)} " if len(lista) == 5 else ""
        resumo_tiers = ", ".join([f"T{t} (<code>#{cid}</code> x{qtd})" for cid, t, qtd in lista])
        linhas.append(f"{selo}<b>{esc(nome)}</b> [{len(lista)}/{total_existentes}]: {resumo_tiers}")

    linhas.append("\n<i>Use /artista nome para ver detalhes ou /ranking nome para ver o top jogadores.</i>")
    await msg.reply_text("\n".join(linhas)[:4000], parse_mode="HTML")

async def _mostrar_colecao(target, user_id: int, username: str | None = None, page: int = 0, tag_name: str | None = None, callback_prefix: str = "colecao"):
    conn=get_db(); cur=conn.cursor()
    if tag_name:
        cur.execute("SELECT c.id,c.nome,c.tier,i.quantidade FROM inventario i JOIN cartas c ON c.id=i.carta_id JOIN carta_tags ct ON ct.carta_id=c.id JOIN tags t ON t.id=ct.tag_id WHERE i.user_id=%s AND LOWER(t.nome)=LOWER(%s) ORDER BY c.nome,c.tier,c.id",(user_id,tag_name))
    else:
        cur.execute("SELECT c.id,c.nome,c.tier,i.quantidade FROM inventario i JOIN cartas c ON c.id=i.carta_id WHERE i.user_id=%s ORDER BY c.nome,c.tier,c.id",(user_id,))
    rows=cur.fetchall()
    cur.execute("SELECT LOWER(nome),COUNT(*) FROM cartas GROUP BY LOWER(nome)")
    total_tiers=dict(cur.fetchall()); cur.close(); conn.close()
    tags_por_carta=_tags_por_carta([row[0] for row in rows])
    apelido,_=obter_apelido(user_id,username or "")
    if not rows:
        text=f"Sua coleção está vazia. Use /girar para pegar sua primeira carta!"
        markup=None
    else:
        grouped={}
        for cid,nome,tier,qty in rows: grouped.setdefault(nome,[]).append((cid,tier,qty))
        entries=list(grouped.items()); size=8; pages=max(1,(len(entries)+size-1)//size); page=max(0,min(page,pages-1))
        titulo_colecao=f"Coleção #{esc(tag_name)}" if tag_name else f"Coleção de {esc(apelido)}"
        lines=[f"🫧 <b>{titulo_colecao}</b> · página {page+1}/{pages}\n"]
        for name, variants in entries[page*size:(page+1)*size]:
            n_total=total_tiers.get(name.lower(),len(variants))
            versions=", ".join(f"T{t} (<code>#{cid}</code> ×{qty})" for cid,t,qty in variants)
            lines.append(f"<b>{esc(name)}</b> [{len(variants)}/{n_total}]: {versions}")
            tags_text=_texto_tags([cid for cid,_,_ in variants],tags_por_carta)
            if tags_text: lines.append(f"  {tags_text}")
        lines.append("\n<i>Use /artista nome ou /ranking nome para ver progresso e ranking.</i>")
        buttons=[]
        if pages>1:
            nav=[]
            if page: nav.append(InlineKeyboardButton("⬅️",callback_data=f"{callback_prefix}|{page-1}|{user_id}"+(f"|{tag_name}" if tag_name else "")))
            nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data=f"{callback_prefix}|noop"))
            if page+1<pages: nav.append(InlineKeyboardButton("➡️",callback_data=f"{callback_prefix}|{page+1}|{user_id}"+(f"|{tag_name}" if tag_name else "")))
            buttons.append(nav)
        markup=InlineKeyboardMarkup(buttons) if buttons else None; text="\n".join(lines)
    if hasattr(target,"edit_message_text"):
        await target.edit_message_text(text,parse_mode="HTML",reply_markup=markup)
    else:
        await target.reply_text(text,parse_mode="HTML",reply_markup=markup)

async def callback_colecao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; p=q.data.split("|")
    if p[1]=="noop": await q.answer(); return
    user_id=int(p[2])
    if q.from_user.id!=user_id:
        await q.answer("Abra /colecao para ver sua coleção.",show_alert=True); return
    await q.answer(); await _mostrar_colecao(q,user_id,q.from_user.username,int(p[1]))

async def callback_col_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; p=q.data.split("|")
    if p[1]=="noop": await q.answer(); return
    if len(p)==3:
        user_id=int(p[2])
        if q.from_user.id!=user_id:
            await q.answer("Abra /colecao para ver sua coleção.",show_alert=True); return
        await q.answer(); await _mostrar_colecao(q,user_id,q.from_user.username,int(p[1])); return
    if len(p)<4: await q.answer(); return
    user_id=int(p[2]); tag_name=p[3]
    if q.from_user.id!=user_id:
        await q.answer("Abra /col para ver sua coleção.",show_alert=True); return
    await q.answer(); await _mostrar_colecao(q,user_id,q.from_user.username,int(p[1]),tag_name=tag_name,callback_prefix="col")

async def cmd_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message; user=update.effective_user
    if not msg or not user: return
    name=" ".join(context.args).strip()
    if not name:
        await msg.reply_text("Use <code>/tag nome-da-tag</code> para ver Minha coleção e Catálogo.",parse_mode="HTML"); return
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT id,nome FROM tags WHERE LOWER(nome)=LOWER(%s)",(name,)); row=cur.fetchone(); cur.close(); conn.close()
    if not row:
        await msg.reply_text(f"Não encontrei a tag <b>{esc(name)}</b>.",parse_mode="HTML"); return
    tid,tag=row
    buttons=[[InlineKeyboardButton("♻️ Minha coleção",callback_data=f"tag|mine|{tid}|{user.id}|0"),InlineKeyboardButton("📚 Catálogo",callback_data=f"tag|catalog|{tid}|{user.id}|0")]]
    await msg.reply_text(f"🏷️ <b>Tag: {esc(tag)}</b>\nEscolha o que deseja consultar:",reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")

async def callback_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; p=q.data.split("|")
    if len(p)<5 or p[1]=="noop": await q.answer(); return
    mode,tag_id,user_id,page=p[1],int(p[2]),int(p[3]),int(p[4])
    if q.from_user.id!=user_id:
        await q.answer("Abra /tag para consultar suas opções.",show_alert=True); return
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT nome FROM tags WHERE id=%s",(tag_id,)); row=cur.fetchone()
    if not row:
        cur.close(); conn.close(); await q.answer("Essa tag não existe mais.",show_alert=True); return
    tag=row[0]
    if mode=="mine":
        cur.close(); conn.close(); await q.answer(); await _mostrar_colecao(q,user_id,q.from_user.username,page,tag_name=tag,callback_prefix="col"); return
    cur.execute("SELECT c.id,c.nome,c.tier,COALESCE(SUM(i.quantidade) FILTER (WHERE i.user_id=%s),0) FROM cartas c JOIN carta_tags ct ON ct.carta_id=c.id LEFT JOIN inventario i ON i.carta_id=c.id WHERE ct.tag_id=%s GROUP BY c.id,c.nome,c.tier ORDER BY c.nome,c.tier,c.id",(user_id,tag_id))
    rows=cur.fetchall(); cur.close(); conn.close(); size=8; pages=max(1,(len(rows)+size-1)//size); page=max(0,min(page,pages-1)); rows_page=rows[page*size:(page+1)*size]
    lines=[f"📚 <b>Catálogo da tag {esc(tag)}</b> · {len(rows)} carta(s) · página {page+1}/{pages}\n"]
    lines.extend(f"• <code>#{cid}</code> {esc(name)} (T{tier})" for cid,name,tier,_ in rows_page)
    tags_by_card=_tags_por_carta([r[0] for r in rows_page])
    for cid,name,tier,owned in rows_page:
        tags_text=_texto_tags([cid],tags_by_card)
        if tags_text: lines.append(f"  {tags_text}")
    for cid,name,tier,owned in rows_page:
        if owned: lines.append(f"  ↳ Você possui {owned}x da carta #{cid}.")
    if not rows: lines.append("Nenhuma carta cadastrada com essa tag.")
    buttons=[]
    if pages>1:
        nav=[]
        if page: nav.append(InlineKeyboardButton("⬅️",callback_data=f"tag|catalog|{tag_id}|{user_id}|{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data="tag|noop"))
        if page+1<pages: nav.append(InlineKeyboardButton("➡️",callback_data=f"tag|catalog|{tag_id}|{user_id}|{page+1}"))
        buttons.append(nav)
    await q.answer(); await q.edit_message_text("\n".join(lines),parse_mode="HTML",reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

def _tagadm_apply_names(tag_id: int, names: list[str], remove: bool = False):
    conn=get_db(); cur=conn.cursor(); changed=0; skipped=[]; missing=[]
    for name in names:
        cur.execute("SELECT id FROM cartas WHERE LOWER(TRIM(nome))=LOWER(TRIM(%s))",(name,)); card_ids=[r[0] for r in cur.fetchall()]
        if not card_ids:
            missing.append(name); continue
        if remove:
            cur.execute("DELETE FROM carta_tags WHERE tag_id=%s AND carta_id=ANY(%s)",(tag_id,card_ids)); changed+=cur.rowcount
            continue
        for cid in card_ids:
            cur.execute("SELECT COUNT(*) FROM carta_tags WHERE carta_id=%s",(cid,)); count=cur.fetchone()[0]
            cur.execute("SELECT 1 FROM carta_tags WHERE carta_id=%s AND tag_id=%s",(cid,tag_id)); exists=cur.fetchone()
            if exists: continue
            if count>=3:
                skipped.append(name); continue
            cur.execute("INSERT INTO carta_tags(carta_id,tag_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(cid,tag_id)); changed+=cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return changed,sorted(set(skipped),key=str.lower),missing

async def _tagadm_home(target, page: int = 0):
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT t.id,t.nome,COUNT(DISTINCT LOWER(c.nome)) FROM tags t LEFT JOIN carta_tags ct ON ct.tag_id=t.id LEFT JOIN cartas c ON c.id=ct.carta_id GROUP BY t.id,t.nome ORDER BY LOWER(t.nome)")
    rows=cur.fetchall(); cur.close(); conn.close(); size=10; pages=max(1,(len(rows)+size-1)//size); page=max(0,min(page,pages-1))
    buttons=[[InlineKeyboardButton(f"🏷️ {name} · {count} nomes",callback_data=f"tagadm|open|{tid}|0")] for tid,name,count in rows[page*size:(page+1)*size]]
    buttons.append([InlineKeyboardButton("➕ Criar tag",callback_data="tagadm|create")])
    nav=[]
    if page: nav.append(InlineKeyboardButton("⬅️",callback_data=f"tagadm|home|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data="tagadm|noop"))
    if page+1<pages: nav.append(InlineKeyboardButton("➡️",callback_data=f"tagadm|home|{page+1}"))
    buttons.append(nav)
    text="🏷️ <b>Gerenciar tags</b>\nEscolha uma tag ou crie uma nova. Você pode aplicar uma tag a vários nomes de uma vez."
    if hasattr(target,"edit_message_text"): await target.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")
    else: await target.reply_text(text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")

async def _tagadm_cards(q, tag_id: int, page: int = 0, search: str | None = None, answer_query: bool = True):
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT nome FROM tags WHERE id=%s",(tag_id,)); tagrow=cur.fetchone()
    if not tagrow:
        cur.close(); conn.close(); await q.answer("Tag não encontrada.",show_alert=True); return
    tag=tagrow[0]
    args=[tag_id]; where=""
    if search:
        where="WHERE c.nome ILIKE %s"; args.append(f"%{search}%")
    cur.execute(f"SELECT MIN(c.id),MIN(c.nome),COUNT(*),COUNT(ct.carta_id) FROM cartas c LEFT JOIN carta_tags ct ON ct.carta_id=c.id AND ct.tag_id=%s {where} GROUP BY LOWER(c.nome) ORDER BY LOWER(MIN(c.nome))",tuple(args))
    rows=cur.fetchall(); cur.close(); conn.close(); size=10; pages=max(1,(len(rows)+size-1)//size); page=max(0,min(page,pages-1)); chunk=rows[page*size:(page+1)*size]
    buttons=[]
    for cid,name,versions,tagged in chunk:
        mark="✅" if tagged else "▫️"
        buttons.append([InlineKeyboardButton(f"{mark} {name[:42]} · {versions} v.",callback_data=f"tagadm|toggle|{tag_id}|{cid}|{page}")])
    buttons.append([InlineKeyboardButton("📋 Aplicar a lista de nomes",callback_data=f"tagadm|bulk|{tag_id}"),InlineKeyboardButton("➖ Remover por nome",callback_data=f"tagadm|remove|{tag_id}")])
    buttons.append([InlineKeyboardButton("🔎 Pesquisar nomes",callback_data=f"tagadm|search|{tag_id}")])
    nav=[]
    if page: nav.append(InlineKeyboardButton("⬅️",callback_data=f"tagadm|open|{tag_id}|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data="tagadm|noop"))
    if page+1<pages: nav.append(InlineKeyboardButton("➡️",callback_data=f"tagadm|open|{tag_id}|{page+1}"))
    buttons.append(nav); buttons.append([InlineKeyboardButton("🔙 Tags",callback_data="tagadm|home|0")])
    search_label=f"\nPesquisa: <i>{esc(search)}</i>" if search else ""
    text=f"🏷️ <b>{esc(tag)}</b> · {len(rows)} nomes · página {page+1}/{pages}{search_label}\nMarque ou desmarque os nomes pelos botões; a ação vale para todas as versões do nome."
    if hasattr(q,"answer"):
        if answer_query: await q.answer()
        await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")
    else:
        await q.reply_text(text,reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")

async def cmd_tags_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_editor(update.effective_user.id):
        await update.effective_message.reply_text("Apenas a equipe pode gerenciar tags."); return
    await _tagadm_home(update.effective_message,0)

async def callback_tagadm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; p=q.data.split("|"); action=p[1]
    if not eh_editor(q.from_user.id):
        await q.answer("Apenas a equipe.",show_alert=True); return
    if action=="noop": await q.answer(); return
    if action=="home": await q.answer(); await _tagadm_home(q,int(p[2])); return
    if action=="create":
        context.user_data["tagadm_pending"]={"action":"create"}; await q.answer(); await q.message.reply_text("Digite o nome da tag nova:"); return
    if action=="open":
        tag_id=int(p[2]); await _tagadm_cards(q,tag_id,int(p[3]),context.user_data.get(f"tagadm_search:{tag_id}")); return
    if action=="search":
        context.user_data["tagadm_pending"]={"action":"search","tag_id":int(p[2])}; await q.answer(); await q.message.reply_text("Digite o nome ou trecho do artista/personagem para filtrar a lista:"); return
    if action in ("bulk","remove"):
        context.user_data["tagadm_pending"]={"action":"bulk" if action=="bulk" else "remove_name","tag_id":int(p[2])}
        await q.answer(); await q.message.reply_text("Envie os nomes separados por vírgula, ponto e vírgula ou linhas. A remoção aceita também um único nome."); return
    if action=="toggle":
        tag_id,anchor,page=int(p[2]),int(p[3]),int(p[4]); conn=get_db(); cur=conn.cursor()
        cur.execute("SELECT nome FROM cartas WHERE id=%s",(anchor,)); row=cur.fetchone()
        if not row:
            cur.close(); conn.close(); await q.answer("Carta não encontrada.",show_alert=True); return
        name=row[0]; cur.execute("SELECT id FROM cartas WHERE LOWER(nome)=LOWER(%s)",(name,)); ids=[r[0] for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) FROM carta_tags WHERE carta_id=ANY(%s) AND tag_id=%s",(ids,tag_id)); exists=cur.fetchone()[0]>0
        if exists:
            cur.execute("DELETE FROM carta_tags WHERE carta_id=ANY(%s) AND tag_id=%s",(ids,tag_id)); note="Tag removida desse nome."
        else:
            changed=0; skipped=False
            for cid in ids:
                cur.execute("SELECT COUNT(*) FROM carta_tags WHERE carta_id=%s",(cid,)); count=cur.fetchone()[0]
                if count>=3: skipped=True; continue
                cur.execute("INSERT INTO carta_tags(carta_id,tag_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(cid,tag_id)); changed+=cur.rowcount
            note=f"Tag aplicada a {changed} versão(ões)."+(" Algumas versões já atingiram o limite de 3 tags." if skipped else "")
        conn.commit(); cur.close(); conn.close(); await q.answer(note,show_alert=True); await _tagadm_cards(q,tag_id,page,context.user_data.get(f"tagadm_search:{tag_id}"),answer_query=False); return

async def _handler_texto_tagadm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending=context.user_data.get("tagadm_pending"); msg=update.effective_message
    if not pending or not msg or not msg.text: return False
    if not eh_editor(update.effective_user.id): context.user_data.pop("tagadm_pending",None); return True
    action=pending["action"]; value=msg.text.strip()
    if action=="create":
        name=value.lstrip("#").strip()[:24]
        if not name:
            await msg.reply_text("Digite um nome de tag."); return True
        conn=get_db(); cur=conn.cursor(); cur.execute("SELECT id FROM tags WHERE LOWER(nome)=LOWER(%s)",(name,)); row=cur.fetchone()
        if row: tag_id=row[0]
        else:
            cur.execute("INSERT INTO tags(nome) VALUES(%s) RETURNING id",(name,)); tag_id=cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close(); context.user_data.pop("tagadm_pending",None)
        await msg.reply_text(f"Tag <b>{esc(name)}</b> pronta. Agora você pode marcar nomes pelos botões ou colar uma lista.",parse_mode="HTML"); await _tagadm_cards(msg,tag_id,0); return True
    if action=="search":
        context.user_data.pop("tagadm_pending",None); context.user_data[f"tagadm_search:{pending['tag_id']}"]=value; await _tagadm_cards(msg,pending["tag_id"],0,value); return True
    names=[part.strip().lstrip("#") for part in re.split(r"[,;\n{}]+",value) if part.strip()]
    if action=="remove_name" and len(names)>1:
        await msg.reply_text("Para remover por nome, envie um nome por vez; ou use Aplicar a lista para remover vários."); return True
    changed,skipped,missing=_tagadm_apply_names(pending["tag_id"],names,remove=action=="remove_name")
    context.user_data.pop("tagadm_pending",None)
    summary=f"Alterações feitas em {changed} versão(ões)."
    if skipped: summary+=f"\nLimite de 3 tags atingido: {', '.join(skipped[:12])}."
    if missing: summary+=f"\nNão encontrei: {', '.join(missing[:20])}."
    await msg.reply_text(summary)
    await _tagadm_cards(msg,pending["tag_id"],0)
    return True

def _montar_wishlist(user_id: int, dono_id: int | None = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome, c.tier
        FROM wishlists w JOIN cartas c ON c.id = w.carta_id
        WHERE w.user_id = %s ORDER BY c.nome, c.tier, c.id
    """, (user_id,))
    itens = cur.fetchall()
    cur.execute("SELECT apelido, username FROM usuarios WHERE user_id = %s", (user_id,))
    pessoa = cur.fetchone()
    cur.close()
    conn.close()
    tags_by_id=_tags_por_carta([item[0] for item in itens])
    nome = pessoa[0] if pessoa and pessoa[0] else (f"@{pessoa[1]}" if pessoa and pessoa[1] else f"Mixer #{str(user_id)[-4:]}")
    linhas = [renderizar_template(obter_texto_publico("wishlist_titulo"), jogador=esc(nome)) + "\n"]
    if itens:
        for cid,carta_nome,tier in itens:
            tags_text=_texto_tags([cid],tags_by_id)
            linhas.append(f"• <code>#{cid}</code> {esc(carta_nome)} (T{tier})"+(f" · {tags_text}" if tags_text else ""))
    else:
        linhas.append(obter_texto_publico("wishlist_vazia"))
    botoes = []
    if dono_id == user_id:
        botoes.append([
            InlineKeyboardButton("➕ Adicionar carta", callback_data="wish|addpage|0"),
            InlineKeyboardButton("➖ Remover carta", callback_data="wish|rempage|0"),
        ])
        botoes.append([InlineKeyboardButton("🔍 Pesquisar carta", callback_data="wish|search")])
        botoes.append([InlineKeyboardButton("🔄 Atualizar", callback_data="wish|home")])
    else:
        for cid, carta_nome, _ in itens[:20]:
            botoes.append([InlineKeyboardButton(f"Ver #{cid} {carta_nome[:35]}", callback_data=f"wish|show|{cid}")])
    return "\n".join(linhas)[:4000], InlineKeyboardMarkup(botoes) if botoes else None

async def _mostrar_resultados_wishlist(query, context, busca: str, page: int = 0):
    page_size = 12
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cartas WHERE nome ILIKE %s", (f"%{busca}%",))
    total = cur.fetchone()[0]
    total_paginas = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_paginas - 1))
    cur.execute("SELECT id, nome, tier FROM cartas WHERE nome ILIKE %s ORDER BY nome, tier, id LIMIT %s OFFSET %s", (f"%{busca}%", page_size, page * page_size))
    itens = cur.fetchall()
    cur.close()
    conn.close()
    context.user_data["wishlist_search_query"] = busca
    if not itens:
        texto = renderizar_template(obter_texto_publico("wishlist_sem_resultados"), busca=esc(busca))
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data="wish|home")]])
        if hasattr(query, "answer"):
            await query.answer()
            await query.edit_message_text(texto, parse_mode="HTML", reply_markup=markup)
        else:
            await query.reply_text(texto, parse_mode="HTML", reply_markup=markup)
        return
    botoes = []
    linha = []
    for cid, nome, tier in itens:
        linha.append(InlineKeyboardButton(f"#{cid} {nome[:28]} T{tier}", callback_data=f"wish|add|{cid}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    if total_paginas > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"wish|searchpage|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_paginas}", callback_data="wish|noop"))
        if page + 1 < total_paginas:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"wish|searchpage|{page + 1}"))
        botoes.append(nav)
    botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="wish|home")])
    texto = renderizar_template(obter_texto_publico("wishlist_resultados"), busca=esc(busca))
    markup = InlineKeyboardMarkup(botoes)
    if hasattr(query, "answer"):
        await query.answer()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await query.reply_text(texto, reply_markup=markup, parse_mode="HTML")

async def cmd_wishlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    verificar_usuario(user.id, user.username)
    identificador = " ".join(context.args).strip()
    if identificador:
        pessoa = _resolver_jogador(identificador)
        if not pessoa:
            await msg.reply_text("Não encontrei esse jogador. Ele precisa ter iniciado o bot antes.")
            return
        alvo_id = pessoa[0]
    else:
        alvo_id = user.id
    texto, markup = _montar_wishlist(alvo_id, dono_id=user.id if alvo_id == user.id else None)
    context.user_data["wishlist_owner_id"] = user.id
    await msg.reply_text(texto, reply_markup=markup, parse_mode="HTML")

async def callback_wishlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    partes = query.data.split("|")
    acao = partes[1]
    if acao == "show":
        carta_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT file_id, media_type, nome, tier FROM cartas WHERE id = %s", (carta_id,))
        carta = cur.fetchone()
        cur.close()
        conn.close()
        if not carta:
            await query.answer("Carta não encontrada.", show_alert=True)
            return
        await query.answer()
        legenda = f"💧 <b>Wishlist:</b> #{carta_id} {esc(carta[2])} (Tier {carta[3]})"
        await enviar_midia_carta(query.message, carta[0], carta[1], legenda, bot=context.bot)
        return

    if context.user_data.get("wishlist_owner_id") != query.from_user.id:
        await query.answer("Abra sua wishlist com /wishlist para usar esses botões.", show_alert=True)
        return
    if acao == "home":
        texto, markup = _montar_wishlist(query.from_user.id, dono_id=query.from_user.id)
        await query.answer()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return
    if acao == "search":
        context.user_data["wishlist_search_pending"] = True
        await query.answer()
        await query.edit_message_text(obter_texto_publico("wishlist_busca"), parse_mode="HTML")
        return
    if acao == "searchpage":
        busca = context.user_data.get("wishlist_search_query", "")
        page = int(partes[2])
        await _mostrar_resultados_wishlist(query, context, busca, page)
        return
    if acao in ("addpage", "rempage"):
        page = int(partes[2])
        page_size = 12
        conn = get_db()
        cur = conn.cursor()
        if acao == "addpage":
            cur.execute("""
                SELECT COUNT(*) FROM cartas c
                WHERE NOT EXISTS (SELECT 1 FROM wishlists w WHERE w.user_id = %s AND w.carta_id = c.id)
            """, (query.from_user.id,))
            total = cur.fetchone()[0]
            cur.execute("""
                SELECT c.id, c.nome, c.tier FROM cartas c
                WHERE NOT EXISTS (SELECT 1 FROM wishlists w WHERE w.user_id = %s AND w.carta_id = c.id)
                ORDER BY c.nome, c.tier, c.id LIMIT %s OFFSET %s
            """, (query.from_user.id, page_size, page * page_size))
        else:
            cur.execute("SELECT COUNT(*) FROM wishlists WHERE user_id = %s", (query.from_user.id,))
            total = cur.fetchone()[0]
            cur.execute("""
                SELECT c.id, c.nome, c.tier FROM wishlists w JOIN cartas c ON c.id = w.carta_id
                WHERE w.user_id = %s ORDER BY c.nome, c.tier, c.id LIMIT %s OFFSET %s
            """, (query.from_user.id, page_size, page * page_size))
        itens = cur.fetchall()
        cur.close()
        conn.close()
        total_paginas = max(1, (total + page_size - 1) // page_size)
        botoes = []
        linha = []
        for cid, nome, tier in itens:
            label = f"#{cid} {nome[:28]} T{tier}"
            linha.append(InlineKeyboardButton(label, callback_data=f"wish|{'add' if acao == 'addpage' else 'remove'}|{cid}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
        if total_paginas > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️", callback_data=f"wish|{acao}|{page - 1}"))
            nav.append(InlineKeyboardButton(f"{min(page + 1, total_paginas)}/{total_paginas}", callback_data="wish|noop"))
            if page + 1 < total_paginas:
                nav.append(InlineKeyboardButton("➡️", callback_data=f"wish|{acao}|{page + 1}"))
            botoes.append(nav)
        botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="wish|home")])
        chave_prompt = "wishlist_adicionar" if acao == "addpage" else "wishlist_remover"
        texto = obter_texto_publico(chave_prompt)
        await query.answer()
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")
        return
    if acao == "noop":
        await query.answer()
        return
    if acao in ("add", "remove"):
        carta_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        if acao == "add":
            cur.execute("INSERT INTO wishlists (user_id, carta_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (query.from_user.id, carta_id))
        else:
            cur.execute("DELETE FROM wishlists WHERE user_id = %s AND carta_id = %s", (query.from_user.id, carta_id))
        conn.commit()
        cur.close()
        conn.close()
        texto, markup = _montar_wishlist(query.from_user.id, dono_id=query.from_user.id)
        await query.answer("Wishlist atualizada.")
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")

async def cmd_loja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    verificar_usuario(user.id, user.username)
    context.user_data["loja_owner"] = user.id
    await _mostrar_menu_loja(msg, user.id)

async def _mostrar_menu_loja(msg, user_id: int, editar: bool = False):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT aqua_coins, giros_restantes FROM usuarios WHERE user_id = %s", (user_id,))
    row = cur.fetchone() or (0, 0)
    cur.close()
    conn.close()
    texto = renderizar_template(obter_texto_publico("loja_menu"), saldo=row[0], giros=row[1])
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("💿 Vender cartas", callback_data="loja|vender")],
        [InlineKeyboardButton("🎲 Comprar giros", callback_data="loja|comprar")],
    ])
    if editar:
        await msg.edit_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await msg.reply_text(texto, reply_markup=markup, parse_mode="HTML")

def _montar_menu_venda(user_id: int, page: int = 0):
    _, markup, page, total = _montar_botoes_inventario(user_id, "loja", page)
    linhas = [list(linha) for linha in markup.inline_keyboard]
    linhas.insert(-1, [InlineKeyboardButton("🔍 Pesquisar pelo nome", callback_data="loja|search")])
    return InlineKeyboardMarkup(linhas), page, total

async def _mostrar_resultados_venda(query, context, user_id: int, busca: str, page: int = 0):
    page_size = 10
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT c.id FROM inventario i JOIN cartas c ON c.id = i.carta_id
            WHERE i.user_id = %s AND i.quantidade > 0 AND c.nome ILIKE %s
            GROUP BY c.id HAVING SUM(i.quantidade) > 0
        ) itens
    """, (user_id, f"%{busca}%"))
    total = cur.fetchone()[0]
    total_paginas = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_paginas - 1))
    cur.execute("""
        SELECT c.id, c.nome, c.tier, SUM(i.quantidade)
        FROM inventario i JOIN cartas c ON c.id = i.carta_id
        WHERE i.user_id = %s AND i.quantidade > 0 AND c.nome ILIKE %s
        GROUP BY c.id, c.nome, c.tier HAVING SUM(i.quantidade) > 0
        ORDER BY c.nome, c.tier, c.id LIMIT %s OFFSET %s
    """, (user_id, f"%{busca}%", page_size, page * page_size))
    itens = cur.fetchall()
    cur.close()
    conn.close()
    context.user_data["loja_search_query"] = busca
    if not itens:
        texto = renderizar_template(obter_texto_publico("loja_busca_vazia"), busca=esc(busca))
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data="loja|vender")]])
        if hasattr(query, "answer"):
            await query.answer()
            await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        else:
            await query.reply_text(texto, reply_markup=markup, parse_mode="HTML")
        return

    botoes = []
    linha = []
    grupos = {}
    for carta_id, nome, tier, quantidade in itens:
        linha.append(InlineKeyboardButton(f"#{carta_id} {nome[:25]} T{tier} ×{quantidade}", callback_data=f"loja|card|{carta_id}|0"))
        chave = nome.strip().casefold()
        if chave not in grupos:
            grupos[chave] = [carta_id, nome, 0, 0]
        grupos[chave][2] += 1
        grupos[chave][3] += quantidade
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    for carta_id, nome, tiers, quantidade in grupos.values():
        if tiers > 1:
            botoes.append([InlineKeyboardButton(f"Vender todos os tiers • {nome[:24]} ×{quantidade}", callback_data=f"loja|alltiers|{carta_id}")])
    if total_paginas > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"loja|searchpage|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_paginas}", callback_data="loja|noop"))
        if page + 1 < total_paginas:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"loja|searchpage|{page + 1}"))
        botoes.append(nav)
    botoes.append([InlineKeyboardButton("🔍 Nova busca", callback_data="loja|search"), InlineKeyboardButton("⬅️ Voltar", callback_data="loja|vender")])
    texto = f"Cartas suas com <b>{esc(busca)}</b> — escolha uma versão ou todos os tiers:"
    markup = InlineKeyboardMarkup(botoes)
    if hasattr(query, "answer"):
        await query.answer()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await query.reply_text(texto, reply_markup=markup, parse_mode="HTML")

async def _mostrar_resultados_inventario(query, context, user_id: int, prefixo: str, busca: str, page: int = 0):
    page_size = 10
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT c.id) FROM inventario i JOIN cartas c ON c.id=i.carta_id WHERE i.user_id=%s AND i.quantidade>0 AND c.nome ILIKE %s", (user_id, f"%{busca}%"))
    total = cur.fetchone()[0]
    paginas = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, paginas - 1))
    cur.execute("SELECT c.id, c.nome, c.tier, SUM(i.quantidade) FROM inventario i JOIN cartas c ON c.id=i.carta_id WHERE i.user_id=%s AND i.quantidade>0 AND c.nome ILIKE %s GROUP BY c.id,c.nome,c.tier ORDER BY c.nome,c.tier,c.id LIMIT %s OFFSET %s", (user_id, f"%{busca}%", page_size, page * page_size))
    itens = cur.fetchall()
    cur.close()
    conn.close()
    key = f"{prefixo}_search_query"
    context.user_data[key] = busca
    botoes = []
    linha = []
    for cid, nome, tier, qtd in itens:
        linha.append(InlineKeyboardButton(f"#{cid} {nome[:25]} T{tier} ×{qtd}", callback_data=f"{prefixo}|card|{cid}|0"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    if paginas > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefixo}|searchpage|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{paginas}", callback_data=f"{prefixo}|noop"))
        if page + 1 < paginas:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefixo}|searchpage|{page + 1}"))
        botoes.append(nav)
    botoes.append([InlineKeyboardButton("🔍 Nova busca", callback_data=f"{prefixo}|search")])
    teclado = InlineKeyboardMarkup(botoes)
    texto = f"Cartas com <b>{esc(busca)}</b> — escolha uma:"
    if hasattr(query, "answer"):
        await query.answer()
        await query.edit_message_text(texto if itens else "Não encontrei cartas com esse nome.", reply_markup=teclado, parse_mode="HTML")
    else:
        await query.reply_text(texto if itens else "Não encontrei cartas com esse nome.", reply_markup=teclado, parse_mode="HTML")

async def _mostrar_opcoes_venda(query, context, carta_ids: list[int], nome: str, total: int, alltiers_id: int | None = None):
    context.user_data["loja_sale_cards"] = carta_ids
    context.user_data["loja_sale_name"] = nome
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT c.id, c.tier, c.event_id, COALESCE(SUM(i.quantidade),0) FROM cartas c LEFT JOIN inventario i ON i.carta_id=c.id AND i.user_id=%s WHERE c.id=ANY(%s) GROUP BY c.id,c.tier,c.event_id", (context.user_data.get("loja_owner"), carta_ids))
    precos = {cid: (150 if eid is not None else 1000 if tier == 6 else 1, qtd) for cid,tier,eid,qtd in cur.fetchall()}
    cur.close(); conn.close()
    fila_valores = [valor for cid in carta_ids for valor in [precos.get(cid, (1,0))[0]] * precos.get(cid,(1,0))[1]]
    quantidades = sorted({n for n in (1, 2, 3, 5, 10, total) if n <= total})
    botoes = []
    linha = []
    for qtd in quantidades:
        recebido_previsto = sum(fila_valores[:qtd])
        linha.append(InlineKeyboardButton(f"Vender {qtd} (+{recebido_previsto} EccoCoins)", callback_data=f"loja|venderq|{qtd}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    if alltiers_id is not None:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT c.id) FROM cartas c JOIN inventario i ON i.carta_id=c.id WHERE i.user_id=%s AND LOWER(TRIM(c.nome))=LOWER(TRIM(%s)) AND i.quantidade>0", (context.user_data.get("loja_owner"), nome))
        total_tipos = cur.fetchone()[0]
        cur.close()
        conn.close()
        if total_tipos > 1:
            botoes.append([InlineKeyboardButton("Vender todos os tiers", callback_data=f"loja|alltiers|{alltiers_id}")])
    botoes.append([InlineKeyboardButton("✏️ Digitar quantidade", callback_data="loja|custom")])
    botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="loja|vender")])
    texto = f"Você tem {total}x <b>{esc(nome)}</b>. Cada carta comum vale 1 EccoCoin, SS+ vale 1.000 e carta de evento vale 150. Escolha a quantidade:"
    markup = InlineKeyboardMarkup(botoes)
    if hasattr(query, "answer"):
        await query.answer()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await query.reply_text(texto, reply_markup=markup, parse_mode="HTML")

def _vender_copias_por_eccocoins(user_id: int, carta_ids: list[int], quantidade: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM usuarios WHERE user_id = %s FOR UPDATE", (user_id,))
    cur.fetchone()
    cur.execute("SELECT i.carta_id, SUM(i.quantidade), c.tier, c.event_id FROM inventario i JOIN cartas c ON c.id=i.carta_id WHERE i.user_id = %s AND i.carta_id = ANY(%s) GROUP BY i.carta_id,c.tier,c.event_id", (user_id, carta_ids))
    rows = cur.fetchall()
    disponiveis = {cid: qtd for cid, qtd, _, _ in rows}
    valores = {cid: (150 if event_id is not None else 1000 if tier == 6 else 1) for cid, _, tier, event_id in rows}
    if quantidade < 1 or sum(disponiveis.values()) < quantidade:
        conn.rollback()
        cur.close()
        conn.close()
        return False, 0, 0
    restante = quantidade
    recebido = 0
    for carta_id in carta_ids:
        vender = min(restante, disponiveis.get(carta_id, 0))
        if vender:
            if not _retirar_inventario(cur, user_id, carta_id, vender):
                conn.rollback()
                cur.close()
                conn.close()
                return False, 0, 0
            recebido += vender * valores.get(carta_id, 1)
            restante -= vender
        if restante == 0:
            break
    cur.execute("UPDATE usuarios SET aqua_coins = aqua_coins + %s WHERE user_id = %s RETURNING aqua_coins", (recebido, user_id))
    saldo = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return True, saldo, recebido

async def callback_loja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = context.user_data.get("loja_owner")
    if user_id != query.from_user.id:
        await query.answer("Abra /loja para usar seus botões.", show_alert=True)
        return
    partes = query.data.split("|")
    acao = partes[1]
    if acao == "vender":
        markup, _, total = _montar_menu_venda(user_id)
        if total == 0:
            await query.answer("Sua coleção está vazia.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(obter_texto_publico("loja_venda_prompt"), reply_markup=markup, parse_mode="HTML")
        return
    if acao == "page":
        markup, _, _ = _montar_menu_venda(user_id, int(partes[2]))
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=markup)
        return
    if acao == "search":
        context.user_data["loja_search_pending"] = True
        await query.answer()
        await query.edit_message_text(obter_texto_publico("loja_busca_prompt"), parse_mode="HTML")
        return
    if acao == "searchpage":
        busca = context.user_data.get("loja_search_query", "")
        await _mostrar_resultados_venda(query, context, user_id, busca, int(partes[2]))
        return
    if acao == "noop":
        await query.answer()
        return
    if acao == "cancel":
        await query.answer("Menu fechado.")
        await query.edit_message_text("Seleção de venda fechada. Use /loja para abrir novamente.")
        context.user_data.pop("loja_sale_cards", None)
        context.user_data.pop("loja_sale_name", None)
        context.user_data["loja_custom_pending"] = False
        context.user_data["loja_search_pending"] = False
        return
    if acao == "card":
        carta_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT c.nome, c.tier, SUM(i.quantidade) FROM cartas c JOIN inventario i ON i.carta_id=c.id WHERE c.id=%s AND i.user_id=%s GROUP BY c.id, c.nome, c.tier HAVING SUM(i.quantidade)>0", (carta_id, user_id))
        carta = cur.fetchone()
        cur.close()
        conn.close()
        if not carta:
            await query.answer("Essa carta não está mais na sua coleção.", show_alert=True)
            return
        await _mostrar_opcoes_venda(query, context, [carta_id], carta[0], carta[2], alltiers_id=carta_id)
        return
    if acao == "alltiers":
        carta_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM cartas WHERE id = %s", (carta_id,))
        carta = cur.fetchone()
        if carta:
            cur.execute("""
                SELECT c.id, c.nome, c.tier, SUM(i.quantidade)
                FROM cartas c JOIN inventario i ON i.carta_id = c.id
                WHERE i.user_id = %s AND LOWER(TRIM(c.nome)) = LOWER(TRIM(%s))
                GROUP BY c.id, c.nome, c.tier ORDER BY c.tier, c.id
            """, (user_id, carta[0]))
            variantes = cur.fetchall()
        else:
            variantes = []
        cur.close()
        conn.close()
        if not variantes:
            await query.answer("Não encontrei suas cartas desse personagem.", show_alert=True)
            return
        ids = [row[0] for row in variantes]
        total = sum(row[3] for row in variantes)
        await _mostrar_opcoes_venda(query, context, ids, f"{carta[0]} — todos os tiers", total)
        return
    if acao == "venderq":
        quantidade = int(partes[2])
        carta_ids = context.user_data.get("loja_sale_cards", [])
        nome = context.user_data.get("loja_sale_name", "carta")
        sucesso, saldo, recebido = _vender_copias_por_eccocoins(user_id, carta_ids, quantidade)
        if not sucesso:
            await query.answer("Você não tem mais essa quantidade.", show_alert=True)
            return
        context.user_data.pop("loja_sale_cards", None)
        context.user_data.pop("loja_sale_name", None)
        await query.answer("Venda concluída!")
        await query.edit_message_text(
            renderizar_template(obter_texto_publico("loja_vendido"), quantidade=quantidade, carta=esc(nome), saldo=saldo).replace(f"{quantidade} EccoCoins", f"{recebido} EccoCoins"),
            parse_mode="HTML",
        )
        return
    if acao == "custom":
        if not context.user_data.get("loja_sale_cards"):
            await query.answer("Escolha uma carta primeiro.", show_alert=True)
            return
        context.user_data["loja_custom_pending"] = True
        await query.answer()
        await query.edit_message_text(obter_texto_publico("loja_custom_prompt"), parse_mode="HTML")
        return
    if acao == "comprar":
        verificar_usuario(user_id, query.from_user.username)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT aqua_coins FROM usuarios WHERE user_id = %s", (user_id,))
        moedas = cur.fetchone()[0]
        cur.close()
        conn.close()
        max_giros = moedas // 10
        if max_giros < 1:
            await query.answer("Você precisa de 10 EccoCoins para comprar um giro.", show_alert=True)
            return
        quantidades = [n for n in (1, 3, 5, 10) if n <= max_giros]
        botoes = []
        linha = []
        for qtd in quantidades:
            linha.append(InlineKeyboardButton(f"{qtd} giro(s) • {qtd * 10} 🫧", callback_data=f"loja|comprarq|{qtd}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
        if max_giros not in quantidades:
            botoes.append([InlineKeyboardButton(f"Máximo ({max_giros} giros)", callback_data="loja|comprarmax")])
        botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="loja|menu")])
        await query.answer()
        await query.edit_message_text(
            renderizar_template(obter_texto_publico("loja_compra_prompt"), max_giros=max_giros),
            reply_markup=InlineKeyboardMarkup(botoes),
        )
        return
    if acao in ("comprarq", "comprarmax"):
        qtd = None if acao == "comprarmax" else int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT aqua_coins, giros_restantes FROM usuarios WHERE user_id = %s FOR UPDATE", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            cur.close()
            conn.close()
            await query.answer("Conta não encontrada.", show_alert=True)
            return
        moedas, giros = row
        if acao == "comprarmax":
            qtd = moedas // 10
        if qtd < 1 or moedas < qtd * 10:
            conn.rollback()
            cur.close()
            conn.close()
            await query.answer("Saldo insuficiente para essa compra.", show_alert=True)
            return
        saldo = moedas - qtd * 10
        total_giros = giros + qtd
        cur.execute("UPDATE usuarios SET aqua_coins = %s, giros_restantes = %s WHERE user_id = %s", (saldo, total_giros, user_id))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Giros comprados!")
        await query.edit_message_text(
            renderizar_template(obter_texto_publico("loja_comprado"), quantidade=qtd, preco=qtd * 10, saldo=saldo, giros=total_giros),
            parse_mode="HTML",
        )
        return
    if acao == "menu":
        await query.answer()
        await _mostrar_menu_loja(query.message, user_id, editar=True)

async def handler_pesquisas_e_venda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return
    if await handler_banimento_pesquisa(update, context):
        raise ApplicationHandlerStop
    if await _handler_texto_perfil(update, context):
        raise ApplicationHandlerStop
    if await _handler_texto_tagadm(update, context):
        raise ApplicationHandlerStop
    if context.user_data.get("evento_pendente"):
        manager = context.application.bot_data.get("eventos_manager")
        if manager and await manager.processar_texto(update, context):
            raise ApplicationHandlerStop
    busca = msg.text.strip()
    if context.user_data.get("dargiros_pendente"):
        try: amount=int(busca)
        except ValueError:
            await msg.reply_text("Digite um número inteiro positivo."); raise ApplicationHandlerStop
        if amount<=0:
            await msg.reply_text("Digite um número inteiro positivo."); raise ApplicationHandlerStop
        target_id=context.user_data.pop("dargiros_pendente")
        total=await _dar_giros_alvo(context.bot,msg.chat_id,target_id,amount)
        await msg.reply_text(f"✅ +{amount} giros para <code>{target_id}</code>. Total disponível: <b>{total}</b>.",parse_mode="HTML")
        raise ApplicationHandlerStop
    if context.user_data.get("catalogo_search_pending"):
        context.user_data["catalogo_search_pending"] = False
        if not busca:
            await msg.reply_text("Digite um nome para pesquisar no catálogo.")
        else:
            await _mostrar_busca_catalogo(msg, context, busca, 0)
        raise ApplicationHandlerStop
    if context.user_data.get("cm_search_pending"):
        await catmgr_search_text(update, context)
        raise ApplicationHandlerStop
    if context.user_data.get("tradeui_search_pending"):
        context.user_data["tradeui_search_pending"] = False
        fluxo = context.user_data.get("tradeui", {})
        dono_lista = fluxo.get("target_id") if fluxo.get("offered_id") else user.id
        await _mostrar_resultados_inventario(msg, context, dono_lista, "tradeui", busca)
        raise ApplicationHandlerStop
    if context.user_data.get("doarui_search_pending"):
        context.user_data["doarui_search_pending"] = False
        await _mostrar_resultados_inventario(msg, context, user.id, "doarui", busca)
        raise ApplicationHandlerStop
    if context.user_data.get("wishlist_search_pending"):
        context.user_data["wishlist_search_pending"] = False
        if not busca:
            await msg.reply_text("Digite pelo menos uma parte do nome para pesquisar.")
            raise ApplicationHandlerStop
        await _mostrar_resultados_wishlist(msg, context, busca)
        raise ApplicationHandlerStop
    if context.user_data.get("loja_search_pending"):
        context.user_data["loja_search_pending"] = False
        if not busca:
            await msg.reply_text("Digite pelo menos uma parte do nome para pesquisar.")
            raise ApplicationHandlerStop
        await _mostrar_resultados_venda(msg, context, user.id, busca)
        raise ApplicationHandlerStop
    if context.user_data.get("loja_custom_pending"):
        try:
            quantidade = int(busca)
        except ValueError:
            await msg.reply_text("Digite somente um número inteiro positivo.")
            raise ApplicationHandlerStop
        if quantidade < 1:
            await msg.reply_text("A quantidade precisa ser maior que zero.")
            raise ApplicationHandlerStop
        carta_ids = context.user_data.get("loja_sale_cards", [])
        nome = context.user_data.get("loja_sale_name", "carta")
        sucesso, saldo, recebido = _vender_copias_por_eccocoins(user.id, carta_ids, quantidade)
        if not sucesso:
            await msg.reply_text("Você não tem essa quantidade disponível para venda.")
            context.user_data["loja_custom_pending"] = False
            raise ApplicationHandlerStop
        context.user_data["loja_custom_pending"] = False
        context.user_data.pop("loja_sale_cards", None)
        context.user_data.pop("loja_sale_name", None)
        await msg.reply_text(
            renderizar_template(obter_texto_publico("loja_vendido"), quantidade=quantidade, carta=esc(nome), saldo=saldo).replace(f"{quantidade} EccoCoins", f"{recebido} EccoCoins"),
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

async def cmd_carta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    try:
        carta_id = int(context.args[0].replace("#", ""))
    except (IndexError, ValueError):
        await msg.reply_text("Uso: <code>/carta ID</code> (ex: <code>/carta 5</code>)", parse_mode="HTML")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome, cat.id, cat.nome, sub.id, sub.nome, subsub.id, subsub.nome,
               c.tier, c.file_id, c.media_type
        FROM cartas c
        JOIN categorias cat ON c.categoria_id = cat.id
        LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
        LEFT JOIN subsubcategorias subsub ON c.subsubcategoria_id = subsub.id
        WHERE c.id = %s
    """, (carta_id,))
    carta = cur.fetchone()
    cur.close()
    conn.close()

    if not carta:
        await msg.reply_text("Carta não encontrada.")
        return

    cid, nome, cid_cat, nome_cat, cid_sub, nome_sub, cid_subsub, nome_subsub, tier, file_id, media_type = carta
    t_cfg = TIERS_CONFIG[tier]
    t_em = obter_emoji_tier(tier)
    cat_codigo, local_str = formatar_local_carta(cid_cat, nome_cat, cid_sub, nome_sub, cid_subsub, nome_subsub)
    legenda = (
        f"{t_em} <b>{cid}. {esc(nome)}.</b>\n"
        f"💿 <b>{cat_codigo}. {local_str}</b>\n"
        f"🔮 <b>Raridade:</b> {t_cfg['nome']}"
    )
    await enviar_midia_carta(msg, file_id, media_type, legenda, bot=context.bot)

# ==========================================
# SISTEMA DE TROCAS (JOGADORES)
# ==========================================
async def _enviar_carta_por_id(target, user_id: int, carta_id: int, bot):
    conn=get_db(); cur=conn.cursor()
    cur.execute("""
        SELECT c.id,c.nome,cat.id,cat.nome,sub.id,sub.nome,subsub.id,subsub.nome,c.tier,c.file_id,c.media_type,
               COALESCE((SELECT SUM(i.quantidade) FROM inventario i WHERE i.user_id=%s AND i.carta_id=c.id),0)
        FROM cartas c JOIN categorias cat ON cat.id=c.categoria_id
        LEFT JOIN subcategorias sub ON sub.id=c.subcategoria_id
        LEFT JOIN subsubcategorias subsub ON subsub.id=c.subsubcategoria_id WHERE c.id=%s
    """,(user_id,carta_id)); row=cur.fetchone(); cur.close(); conn.close()
    if not row:
        message=target.message if hasattr(target,"message") else target
        await message.reply_text("Carta não encontrada."); return
    cid,nome,cid_cat,cat_nome,sid,sub_nome,ssid,subsub_nome,tier,file_id,media_type,owned=row
    codigo,local=formatar_local_carta(cid_cat,cat_nome,sid,sub_nome,ssid,subsub_nome)
    caption=(f"{obter_emoji_tier(tier)} <b>{cid}. {esc(nome)}</b> · Tier {tier}\n"
             f"💿 <b>{codigo}. {esc(local)}</b>\n♻️ Você possui: <b>{int(owned or 0)}</b>")
    tags_text=_texto_tags([cid])
    if tags_text: caption += "\n"+tags_text
    message=target.message if hasattr(target,"message") else target
    await enviar_midia_carta(message,file_id,media_type,caption,bot=bot)

async def cmd_carta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message; user=update.effective_user
    if not msg or not user: return
    verificar_usuario(user.id,user.username)
    term=" ".join(context.args).strip().lstrip("#")
    if not term:
        await msg.reply_text("Use <code>/carta ID</code> ou <code>/carta nome</code>.",parse_mode="HTML"); return
    if term.isdigit():
        await _enviar_carta_por_id(msg,user.id,int(term),context.bot); return
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT id,nome,tier FROM cartas WHERE nome ILIKE %s ORDER BY nome,tier,id LIMIT 10",(f"%{term}%",)); rows=cur.fetchall(); cur.close(); conn.close()
    if not rows:
        await msg.reply_text(f"Não encontrei cartas com <b>{esc(term)}</b>.",parse_mode="HTML"); return
    buttons=[[InlineKeyboardButton(f"#{cid} {nome[:30]} · T{tier}",callback_data=f"carta|id|{cid}|{user.id}")] for cid,nome,tier in rows]
    await msg.reply_text(f"Resultados para <b>{esc(term)}</b>. Escolha a versão da carta:",reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")

async def callback_carta_busca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; _,action,carta_id,user_id=q.data.split("|")
    if q.from_user.id!=int(user_id):
        await q.answer("Abra sua própria busca com /carta.",show_alert=True); return
    await q.answer(); await _enviar_carta_por_id(q,user_id=int(user_id),carta_id=int(carta_id),bot=context.bot)

def _resolver_jogador(identificador: str):
    valor = identificador.strip().lstrip("@").strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, username, apelido FROM usuarios WHERE LOWER(username) = LOWER(%s) OR LOWER(TRIM(apelido)) = LOWER(%s) LIMIT 1",
        (valor, valor),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def _montar_botoes_inventario(user_id: int, prefixo: str, page: int = 0, itens_por_pagina: int = 10):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT i.carta_id FROM inventario i JOIN cartas c ON c.id=i.carta_id WHERE i.user_id = %s AND (%s <> 'doarui' OR c.event_id IS NULL) GROUP BY i.carta_id HAVING SUM(i.quantidade) > 0
        ) itens
    """, (user_id, prefixo))
    total = cur.fetchone()[0]
    total_paginas = max(1, (total + itens_por_pagina - 1) // itens_por_pagina)
    page = max(0, min(page, total_paginas - 1))
    cur.execute("""
        SELECT c.id, c.nome, c.tier, SUM(i.quantidade)
        FROM inventario i JOIN cartas c ON c.id = i.carta_id
        WHERE i.user_id = %s AND (%s <> 'doarui' OR c.event_id IS NULL)
        GROUP BY c.id, c.nome, c.tier
        HAVING SUM(i.quantidade) > 0
        ORDER BY c.nome, c.tier, c.id
        LIMIT %s OFFSET %s
    """, (user_id, prefixo, itens_por_pagina, page * itens_por_pagina))
    itens = cur.fetchall()
    cur.close()
    conn.close()

    botoes = []
    linha = []
    for carta_id, nome, tier, quantidade in itens:
        titulo = f"#{carta_id} {nome[:27]} T{tier} ×{quantidade}"
        linha.append(InlineKeyboardButton(titulo, callback_data=f"{prefixo}|card|{carta_id}|{page}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    if total_paginas > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefixo}|page|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_paginas}", callback_data=f"{prefixo}|noop"))
        if page + 1 < total_paginas:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefixo}|page|{page + 1}"))
        botoes.append(nav)
    if prefixo in ("tradeui", "doarui"):
        botoes.append([InlineKeyboardButton("🔍 Pesquisar por nome", callback_data=f"{prefixo}|search")])
    botoes.append([InlineKeyboardButton("Cancelar", callback_data=f"{prefixo}|cancel")])
    return itens, InlineKeyboardMarkup(botoes), page, total

def _retirar_inventario(cur, user_id: int, carta_id: int, quantidade: int) -> bool:
    cur.execute(
        "SELECT id, quantidade FROM inventario WHERE user_id = %s AND carta_id = %s ORDER BY id FOR UPDATE",
        (user_id, carta_id),
    )
    rows = cur.fetchall()
    total = sum(q or 0 for _, q in rows)
    if quantidade < 1 or total < quantidade:
        return False
    restante = total - quantidade
    if restante:
        cur.execute("UPDATE inventario SET quantidade = %s WHERE id = %s", (restante, rows[0][0]))
        extras = [row_id for row_id, _ in rows[1:]]
        if extras:
            cur.execute("DELETE FROM inventario WHERE id = ANY(%s)", (extras,))
    else:
        cur.execute("DELETE FROM inventario WHERE user_id = %s AND carta_id = %s", (user_id, carta_id))
    return True

def _adicionar_inventario(cur, user_id: int, carta_id: int, quantidade: int):
    cur.execute(
        "SELECT id, quantidade FROM inventario WHERE user_id = %s AND carta_id = %s ORDER BY id FOR UPDATE",
        (user_id, carta_id),
    )
    rows = cur.fetchall()
    if rows:
        cur.execute(
            "UPDATE inventario SET quantidade = %s WHERE id = %s",
            (sum(q or 0 for _, q in rows) + quantidade, rows[0][0]),
        )
        extras = [row_id for row_id, _ in rows[1:]]
        if extras:
            cur.execute("DELETE FROM inventario WHERE id = ANY(%s)", (extras,))
    else:
        cur.execute(
            "INSERT INTO inventario (user_id, carta_id, quantidade) VALUES (%s, %s, %s)",
            (user_id, carta_id, quantidade),
        )

async def cmd_trocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    remetente = update.effective_user
    if not msg or not remetente:
        return
    verificar_usuario(remetente.id, remetente.username)
    identificador = " ".join(context.args).strip()
    if not identificador and msg.reply_to_message and msg.reply_to_message.from_user:
        alvo = msg.reply_to_message.from_user
        identificador = alvo.username or obter_apelido(alvo.id, alvo.username)[0]
    if not identificador:
        await msg.reply_text(
            "Use <code>/trocar @usuario</code> ou <code>/trocar apelido</code>. Depois escolha as cartas pelos botões.",
            parse_mode="HTML"
        )
        return
    destinatario = _resolver_jogador(identificador)
    if not destinatario:
        await msg.reply_text("Não encontrei esse jogador. Ele precisa ter iniciado o bot antes.")
        return
    destinatario_id, target_username, target_alias = destinatario
    if destinatario_id == remetente.id:
        await msg.reply_text("Você não pode trocar cartas consigo mesmo.")
        return
    itens, markup, _, total = _montar_botoes_inventario(remetente.id, "tradeui")
    if total == 0:
        await msg.reply_text("Sua coleção está vazia; não há carta para oferecer.")
        return
    context.user_data["tradeui"] = {
        "sender_id": remetente.id,
        "target_id": destinatario_id,
        "target_name": (target_alias or (f"@{target_username}" if target_username else str(destinatario_id))),
        "origin_group_id": msg.chat_id if msg.chat.type in ("group", "supergroup") else None,
    }
    await msg.reply_text(
        f"Escolha a carta que você oferece para <b>{esc(context.user_data['tradeui']['target_name'])}</b>:",
        reply_markup=markup,
        parse_mode="HTML",
    )

async def callback_troca_ui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fluxo = context.user_data.get("tradeui")
    if not fluxo or fluxo.get("sender_id") != query.from_user.id:
        await query.answer("Esse menu de troca expirou.", show_alert=True)
        return
    partes = query.data.split("|")
    acao = partes[1]
    if acao == "search":
        context.user_data["tradeui_search_pending"] = True
        await query.answer()
        await query.edit_message_text("Digite o nome da carta que deseja oferecer ou receber:")
        return
    if acao == "searchpage":
        dono_lista = fluxo["target_id"] if fluxo.get("offered_id") else query.from_user.id
        await _mostrar_resultados_inventario(query, context, dono_lista, "tradeui", context.user_data.get("tradeui_search_query", ""), int(partes[2]))
        return
    if acao == "cancel":
        context.user_data.pop("tradeui", None)
        await query.answer("Cancelado.")
        await query.edit_message_text("Proposta de troca cancelada.")
        return
    if acao == "noop":
        await query.answer()
        return
    if acao == "page":
        dono_lista = fluxo["target_id"] if fluxo.get("offered_id") else query.from_user.id
        itens, markup, _, total = _montar_botoes_inventario(dono_lista, "tradeui", int(partes[2]))
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=markup)
        return
    if acao == "card":
        carta_id = int(partes[2])
        if not fluxo.get("offered_id"):
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT c.nome, c.tier, c.event_id FROM cartas c JOIN inventario i ON i.carta_id=c.id WHERE c.id=%s AND i.user_id=%s AND i.quantidade>0 LIMIT 1", (carta_id, query.from_user.id))
            carta = cur.fetchone()
            cur.close()
            conn.close()
            if not carta:
                await query.answer("Essa carta não está mais na sua coleção.", show_alert=True)
                return
            if carta[2] is not None and carta[1] < 4:
                await query.answer("Carta de evento só pode ser trocada por tier 4, tier 5, SS+ ou outra carta de evento.", show_alert=True)
                return
            fluxo["offered_id"] = carta_id
            _, markup, _, total = _montar_botoes_inventario(fluxo["target_id"], "tradeui")
            if total == 0:
                await query.answer("Essa pessoa ainda não tem cartas para trocar.", show_alert=True)
                context.user_data.pop("tradeui", None)
                await query.edit_message_text("Troca cancelada: o destinatário ainda não possui cartas.")
                return
            await query.answer("Carta oferecida selecionada.")
            await query.edit_message_text(
                f"Você oferece <b>{esc(carta[0])}</b> (Tier {carta[1]}). Escolha a carta que deseja receber de <b>{esc(fluxo['target_name'])}</b>:",
                reply_markup=markup,
                parse_mode="HTML",
            )
            return

        pedido_id = carta_id
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT c.nome, c.tier, c.event_id FROM cartas c JOIN inventario i ON i.carta_id=c.id WHERE c.id=%s AND i.user_id=%s AND i.quantidade>0 LIMIT 1", (pedido_id, fluxo["target_id"]))
        pedida = cur.fetchone()
        cur.execute("SELECT nome, tier FROM cartas WHERE id = %s", (fluxo["offered_id"],))
        oferecida = cur.fetchone()
        if not pedida or not oferecida:
            cur.close()
            conn.close()
            await query.answer("Uma das cartas não está mais disponível.", show_alert=True)
            context.user_data.pop("tradeui", None)
            return
        cur.execute("SELECT event_id FROM cartas WHERE id=%s", (fluxo["offered_id"]))
        evento_oferecida = cur.fetchone()[0]
        evento_pedida = pedida[2]
        if (evento_oferecida is not None and evento_pedida is None and pedida[1] < 4) or (evento_pedida is not None and evento_oferecida is None and oferecida[1] < 4):
            cur.close(); conn.close()
            await query.answer("Carta de evento só pode ser trocada por tier 4, tier 5, SS+ ou outra carta de evento.", show_alert=True)
            return
        cur.execute(
            "INSERT INTO trocas (remetente_id, destinatario_id, carta_oferecida_id, carta_pedida_id, origin_chat_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (query.from_user.id, fluxo["target_id"], fluxo["offered_id"], pedido_id, fluxo.get("origin_group_id")),
        )
        troca_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        apelido_sender, _ = obter_apelido(query.from_user.id, query.from_user.username)
        texto = renderizar_template(
            obter_texto_publico("troca_proposta"),
            troca_id=troca_id,
            remetente=esc(apelido_sender),
            oferecida=esc(oferecida[0]),
            tier_oferecida=oferecida[1],
            pedida=esc(pedida[0]),
            tier_pedida=pedida[1],
        )
        botoes = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Aceitar", callback_data=f"trade|aceitar|{troca_id}"),
            InlineKeyboardButton("❌ Recusar", callback_data=f"trade|recusar|{troca_id}"),
        ]])
        try:
            await context.bot.send_message(chat_id=fluxo["target_id"], text=texto, reply_markup=botoes, parse_mode="HTML")
        except Exception:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("UPDATE trocas SET status = 'cancelada' WHERE id = %s", (troca_id,))
            conn.commit()
            cur.close()
            conn.close()
            await query.answer("Não consegui enviar a proposta. Peça ao destinatário para abrir o bot com /start.", show_alert=True)
            context.user_data.pop("tradeui", None)
            return
        context.user_data.pop("tradeui", None)
        await query.answer("Proposta enviada.")
        await query.edit_message_text(
            renderizar_template(obter_texto_publico("troca_enviada"), destinatario=esc(fluxo["target_name"])),
            parse_mode="HTML",
        )

async def _avisar_troca_encerrada(bot, troca_id: int, rem_id: int, dest_id: int, origin_chat_id, status: str):
    texto_privado = renderizar_template(obter_texto_publico("troca_status_privado"), troca_id=troca_id, status=esc(status))
    # O destinatário já recebe o resultado editado no privado; envie a cópia ao proponente.
    try:
        await bot.send_message(chat_id=rem_id, text=texto_privado, parse_mode="HTML")
    except Exception:
        pass
    if origin_chat_id:
        nome_rem, _ = obter_apelido(rem_id, None)
        nome_dest, _ = obter_apelido(dest_id, None)
        try:
            await bot.send_message(
                chat_id=origin_chat_id,
                text=renderizar_template(obter_texto_publico("troca_status_grupo"), remetente=esc(nome_rem), destinatario=esc(nome_dest), troca_id=troca_id, status=esc(status)),
                parse_mode="HTML",
            )
        except Exception:
            pass

async def callback_troca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, acao, troca_id_str = query.data.split("|")
    troca_id = int(troca_id_str)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT remetente_id, destinatario_id, carta_oferecida_id, carta_pedida_id, status, origin_chat_id FROM trocas WHERE id = %s FOR UPDATE", (troca_id,))
    troca = cur.fetchone()

    if not troca or troca[4] != "pendente":
        await query.answer("Essa troca já foi encerrada.", show_alert=True)
        cur.close()
        conn.close()
        return

    rem_id, dest_id, c_oferecida, c_pedida, _, origin_chat_id = troca

    if acao == "recusar":
        if query.from_user.id != dest_id:
            await query.answer("Apenas o destinatário pode recusar essa proposta.", show_alert=True)
            cur.close()
            conn.close()
            return
        cur.execute("UPDATE trocas SET status = 'recusada' WHERE id = %s", (troca_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Troca recusada.")
        await query.edit_message_text(renderizar_template(obter_texto_publico("troca_recusada"), troca_id=troca_id), parse_mode="HTML")
        await _avisar_troca_encerrada(context.bot, troca_id, rem_id, dest_id, origin_chat_id, "foi recusada")
        return

    if query.from_user.id != dest_id:
        await query.answer("Apenas quem recebeu a proposta pode aceitar!", show_alert=True)
        cur.close()
        conn.close()
        return

    cur.execute("SAVEPOINT trade_transfer")
    if not _retirar_inventario(cur, rem_id, c_oferecida, 1) or not _retirar_inventario(cur, dest_id, c_pedida, 1):
        cur.execute("ROLLBACK TO SAVEPOINT trade_transfer")
        cur.execute("UPDATE trocas SET status = 'cancelada' WHERE id = %s", (troca_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Falta uma das cartas no inventário!", show_alert=True)
        await query.edit_message_text(f"⚠️ Troca #{troca_id} cancelada por falta de cartas.")
        return
    cur.execute("RELEASE SAVEPOINT trade_transfer")

    _adicionar_inventario(cur, dest_id, c_oferecida, 1)
    _adicionar_inventario(cur, rem_id, c_pedida, 1)
    cur.execute("UPDATE trocas SET status = 'aceita' WHERE id = %s", (troca_id,))
    conn.commit()
    cur.close()
    conn.close()

    await query.answer("Troca concluída!")
    await query.edit_message_text(renderizar_template(obter_texto_publico("troca_aceita"), troca_id=troca_id), parse_mode="HTML")
    await _avisar_troca_encerrada(context.bot, troca_id, rem_id, dest_id, origin_chat_id, "foi aceita")

async def cmd_doar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    verificar_usuario(user.id, user.username)
    identificador = " ".join(context.args).strip()
    if not identificador and msg.reply_to_message and msg.reply_to_message.from_user:
        alvo = msg.reply_to_message.from_user
        identificador = alvo.username or obter_apelido(alvo.id, alvo.username)[0]
    if not identificador:
        await msg.reply_text("Use <code>/doar @usuario</code> ou <code>/doar apelido</code> e escolha a carta e a quantidade nos botões.", parse_mode="HTML")
        return
    destinatario = _resolver_jogador(identificador)
    if not destinatario:
        await msg.reply_text("Não encontrei esse jogador. Ele precisa ter iniciado o bot antes.")
        return
    destino_id, username, apelido = destinatario
    if destino_id == user.id:
        await msg.reply_text("Você não pode doar cartas para si mesmo.")
        return
    itens, markup, _, total = _montar_botoes_inventario(user.id, "doarui")
    if total == 0:
        await msg.reply_text("Sua coleção está vazia; não há carta para doar.")
        return
    nome_destino = apelido or (f"@{username}" if username else str(destino_id))
    context.user_data["doarui"] = {
        "sender_id": user.id, "target_id": destino_id, "target_name": nome_destino,
        "origin_group_id": msg.chat_id if msg.chat.type in ("group", "supergroup") else None,
    }
    await msg.reply_text(
        renderizar_template(obter_texto_publico("doacao_prompt"), destinatario=esc(nome_destino)),
        reply_markup=markup,
        parse_mode="HTML",
    )

async def callback_doarui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fluxo = context.user_data.get("doarui")
    if not fluxo or fluxo.get("sender_id") != query.from_user.id:
        await query.answer("Esse menu de doação expirou.", show_alert=True)
        return
    partes = query.data.split("|")
    acao = partes[1]
    if acao == "search":
        context.user_data["doarui_search_pending"] = True
        await query.answer()
        await query.edit_message_text("Digite o nome da carta que deseja doar:")
        return
    if acao == "searchpage":
        await _mostrar_resultados_inventario(query, context, query.from_user.id, "doarui", context.user_data.get("doarui_search_query", ""), int(partes[2]))
        return
    if acao == "cancel":
        context.user_data.pop("doarui", None)
        await query.answer("Cancelado.")
        await query.edit_message_text("Doação cancelada.")
        return
    if acao == "noop":
        await query.answer()
        return
    if acao == "page":
        dono_lista = query.from_user.id if not fluxo.get("carta_id") else query.from_user.id
        _, markup, _, _ = _montar_botoes_inventario(dono_lista, "doarui", int(partes[2]))
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=markup)
        return
    if acao == "card":
        carta_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT c.nome, c.tier, SUM(i.quantidade), c.event_id FROM cartas c JOIN inventario i ON i.carta_id=c.id WHERE c.id=%s AND i.user_id=%s GROUP BY c.id, c.nome, c.tier HAVING SUM(i.quantidade)>0", (carta_id, query.from_user.id))
        carta = cur.fetchone()
        cur.close()
        conn.close()
        if not carta:
            await query.answer("Essa carta não está mais na sua coleção.", show_alert=True)
            return
        if carta[3] is not None:
            await query.answer("Cartas de evento não podem ser doadas.", show_alert=True)
            return
        fluxo["carta_id"] = carta_id
        fluxo["carta_nome"] = carta[0]
        fluxo["max_qtd"] = carta[2]
        valores = sorted({min(n, carta[2]) for n in (1, 2, 3, 5, 10) if carta[2] >= n})
        valores.append(carta[2])
        botoes = []
        linha = []
        for qtd in dict.fromkeys(valores):
            linha.append(InlineKeyboardButton(f"Doar {qtd}", callback_data=f"doarui|qty|{qtd}"))
            if len(linha) == 3:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
        botoes.append([InlineKeyboardButton("Cancelar", callback_data="doarui|cancel")])
        await query.answer()
        await query.edit_message_text(
            renderizar_template(obter_texto_publico("doacao_quantidade"), carta=esc(carta[0]), quantidade=carta[2]),
            reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML",
        )
        return
    if acao == "qty":
        quantidade = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM cartas WHERE id = %s", (fluxo["carta_id"],))
        carta = cur.fetchone()
        sucesso = carta and _retirar_inventario(cur, query.from_user.id, fluxo["carta_id"], quantidade)
        if not sucesso:
            conn.rollback()
            cur.close()
            conn.close()
            await query.answer("Você não tem mais essa quantidade.", show_alert=True)
            context.user_data.pop("doarui", None)
            return
        _adicionar_inventario(cur, fluxo["target_id"], fluxo["carta_id"], quantidade)
        conn.commit()
        cur.close()
        conn.close()
        nome_destino = fluxo["target_name"]
        nome_remetente = obter_apelido(query.from_user.id, query.from_user.username)[0]
        avisou_destinatario = True
        try:
            await context.bot.send_message(
                chat_id=fluxo["target_id"],
                text=renderizar_template(
                    obter_texto_publico("doacao_recebida"),
                    quantidade=quantidade,
                    carta=esc(carta[0]),
                    remetente=esc(nome_remetente),
                ),
                parse_mode="HTML",
            )
        except Exception:
            avisou_destinatario = False
        if fluxo.get("origin_group_id"):
            try:
                await context.bot.send_message(
                    chat_id=query.from_user.id,
                    text=renderizar_template(obter_texto_publico("doacao_privado_enviada"), quantidade=quantidade, carta=esc(carta[0]), destinatario=esc(nome_destino)),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        if fluxo.get("origin_group_id"):
            try:
                await context.bot.send_message(
                    chat_id=fluxo["origin_group_id"],
                    text=renderizar_template(obter_texto_publico("doacao_grupo"), remetente=esc(nome_remetente), destinatario=esc(nome_destino), quantidade=quantidade, carta=esc(carta[0])),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        context.user_data.pop("doarui", None)
        await query.answer("Doação concluída!")
        aviso = "" if avisou_destinatario else "\nNão consegui avisar em particular; peça para a pessoa abrir o bot com /start."
        texto_sucesso = renderizar_template(
            obter_texto_publico("doacao_enviada"),
            quantidade=quantidade,
            carta=esc(carta[0]),
            destinatario=esc(nome_destino),
        )
        await query.edit_message_text(f"{texto_sucesso}{aviso}", parse_mode="HTML")

# ==========================================
# ASSISTENTE INTERATIVO /addcarta (SUPORTE A 3 NÍVEIS)
# ==========================================
def montar_teclado_categorias():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM categorias WHERE event_id IS NULL ORDER BY id ASC")
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

def montar_teclado_subsubcategorias(sub_id: int, sub_nome: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM subsubcategorias WHERE subcategoria_id = %s ORDER BY id ASC", (sub_id,))
    subsubs = cur.fetchall()
    cur.close()
    conn.close()

    botoes = []
    linha = []
    for ssid, ssnome in subsubs:
        linha.append(InlineKeyboardButton(ssnome, callback_data=f"wiz_subsub|{ssid}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("➕ Criar nova sub-subcategoria (grupo)", callback_data="wiz_new_subsub")])
    botoes.append([InlineKeyboardButton(f"➡️ Sem grupo (direto em {sub_nome})", callback_data="wiz_no_subsub")])
    botoes.append([InlineKeyboardButton("❌ Cancelar", callback_data="wiz_cancel")])
    return InlineKeyboardMarkup(botoes)

def montar_teclado_nomes(context: ContextTypes.DEFAULT_TYPE):
    cat_id = context.user_data["cat_id"]
    sub_id = context.user_data.get("sub_id")
    subsub_id = context.user_data.get("subsub_id")

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
    elif subsub_id is None:
        cur.execute("""
            SELECT MIN(nome), COUNT(*)
            FROM cartas
            WHERE categoria_id = %s AND subcategoria_id = %s AND subsubcategoria_id IS NULL
            GROUP BY LOWER(nome)
            ORDER BY MIN(nome) ASC
        """, (cat_id, sub_id))
    else:
        cur.execute("""
            SELECT MIN(nome), COUNT(*)
            FROM cartas
            WHERE categoria_id = %s AND subcategoria_id = %s AND subsubcategoria_id = %s
            GROUP BY LOWER(nome)
            ORDER BY MIN(nome) ASC
        """, (cat_id, sub_id, subsub_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    context.user_data["nomes_lista"] = [r[0] for r in rows]

    botoes = []
    linha = []
    for idx, (nome_art, qtd_tiers) in enumerate(rows):
        selo = ("✅" if qtd_tiers >= 5 else f"{qtd_tiers}/5") if categoria_usa_tiers(cat_id) else "tier único"
        linha.append(InlineKeyboardButton(f"{nome_art} ({selo})", callback_data=f"wiz_nome|{idx}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("➕ Criar novo nome", callback_data="wiz_new_nome")])
    if sub_id is not None and subsub_id is None:
        botoes.append([InlineKeyboardButton("🏷️ Criar sub-subcategoria (grupo)", callback_data="wiz_new_subsub_from_nome")])
    botoes.append([InlineKeyboardButton("❌ Cancelar", callback_data="wiz_cancel")])
    return InlineKeyboardMarkup(botoes)

async def _wiz_start_common(update: Update, context: ContextTypes.DEFAULT_TYPE, event_id=None, event_name=None):
    if not eh_admin(update.effective_user.id):
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["admin_id"] = update.effective_user.id
    context.user_data["event_id"] = context.user_data.get("event_context_id", event_id)
    if event_id is not None:
        context.user_data["event_context_id"] = event_id
        context.user_data["event_name"] = event_name or "evento"

    texto = "💿 <b>Nova Carta — Passo 1/5</b>\n\nEscolha a <b>categoria</b> ou crie uma nova:"
    markup = montar_teclado_categorias()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return ST_CAT

async def wiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        event_key=" ".join(context.args).strip()
        conn=get_db(); cur=conn.cursor(); cur.execute("SELECT id,nome FROM eventos WHERE LOWER(slug)=LOWER(%s) OR LOWER(nome)=LOWER(%s) ORDER BY id LIMIT 1",(event_key.lower().replace(" ","_"),event_key)); event=cur.fetchone(); cur.close(); conn.close()
        if event: return await _wiz_start_common(update,context,event[0],event[1])
    return await _wiz_start_common(update, context)

async def wiz_start_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q or not eh_admin(q.from_user.id):
        if q: await q.answer("Apenas administradores podem cadastrar cartas de evento.",show_alert=True)
        return ConversationHandler.END
    event_id=int(q.data.split("|")[-1]); conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT nome FROM eventos WHERE id=%s",(event_id,)); row=cur.fetchone(); cur.close(); conn.close()
    if not row:
        await q.answer("Evento não encontrado.",show_alert=True); return ConversationHandler.END
    return await _wiz_start_common(update,context,event_id,row[0])

async def wiz_start_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update.effective_user.id): return ConversationHandler.END
    raw=update.effective_message.text or ""
    match=re.match(r"(?i)^\s*/addcarta[-_]([\w-]+)(?:@[\w]+)?(?:\s|$)",raw)
    slug=match.group(1).lower().replace("-","_") if match else " ".join(context.args).strip().lower().replace(" ","_")
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT id,nome FROM eventos WHERE LOWER(slug)=LOWER(%s) OR LOWER(nome)=LOWER(%s) ORDER BY id LIMIT 1",(slug,slug)); row=cur.fetchone(); cur.close(); conn.close()
    if not row:
        await update.effective_message.reply_text("Não encontrei esse evento. Abra /eventos e use o botão de cadastro de cartas."); return ConversationHandler.END
    return await _wiz_start_common(update,context,row[0],row[1])

async def wiz_start_add_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not eh_admin(q.from_user.id): await q.answer("Apenas administradores.",show_alert=True); return ConversationHandler.END
    carta_id=int(q.data.split("|")[-1]); conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT c.nome,c.categoria_id,cat.nome,c.subcategoria_id,sub.nome,c.subsubcategoria_id,subsub.nome,c.event_id FROM cartas c JOIN categorias cat ON cat.id=c.categoria_id LEFT JOIN subcategorias sub ON sub.id=c.subcategoria_id LEFT JOIN subsubcategorias subsub ON subsub.id=c.subsubcategoria_id WHERE c.id=%s",(carta_id,))
    row=cur.fetchone(); cur.close(); conn.close()
    if not row or row[7] is not None:
        await q.answer("Não é possível adicionar outro tier a essa carta.",show_alert=True); return ConversationHandler.END
    nome,cat_id,cat_nome,sub_id,sub_nome,subsub_id,subsub_nome,_=row
    if not categoria_usa_tiers(cat_id):
        await q.answer("Essa categoria está configurada com tier único.",show_alert=True); return ConversationHandler.END
    context.user_data.clear(); context.user_data.update({"admin_id":q.from_user.id,"nome_carta":nome,"cat_id":cat_id,"cat_nome":cat_nome,"sub_id":sub_id,"sub_nome":sub_nome,"subsub_id":subsub_id,"subsub_nome":subsub_nome,"event_id":None})
    await q.answer(); await q.message.reply_text(f"➕ Adicionando outro tier de <b>{esc(nome)}</b>. Envie a foto/GIF e depois escolha o tier livre.",parse_mode="HTML")
    return ST_MIDIA

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
    cur.execute("SELECT nome, event_id FROM categorias WHERE id = %s", (cat_id,))
    cat_nome, event_id = cur.fetchone()
    if event_id is not None:
        cur.close(); conn.close()
        await query.answer("Selecione uma categoria normal do catálogo.",show_alert=True)
        return ST_CAT
    cur.execute("SELECT COUNT(*) FROM subcategorias WHERE categoria_id = %s", (cat_id,))
    tem_subs = cur.fetchone()[0] > 0
    cur.close()
    conn.close()

    context.user_data["cat_id"] = cat_id
    context.user_data["cat_nome"] = cat_nome
    context.user_data["event_id"] = context.user_data.get("event_context_id", event_id)
    context.user_data["sub_id"] = None
    context.user_data["sub_nome"] = None
    context.user_data["subsub_id"] = None
    context.user_data["subsub_nome"] = None

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
    context.user_data["subsub_id"] = None
    context.user_data["subsub_nome"] = None

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
    cur.execute("SELECT COUNT(*) FROM subsubcategorias WHERE subcategoria_id = %s", (sub_id,))
    tem_subsubs = cur.fetchone()[0] > 0
    cur.close()
    conn.close()

    context.user_data["sub_id"] = sub_id
    context.user_data["sub_nome"] = sub_nome
    context.user_data["subsub_id"] = None
    context.user_data["subsub_nome"] = None

    if tem_subsubs:
        await query.edit_message_text(
            f"📂 <b>{esc(context.user_data['cat_nome'])} • {esc(sub_nome)}</b>\n\n"
            f"Escolha a <b>sub-subcategoria (grupo)</b> ou crie uma nova:",
            reply_markup=montar_teclado_subsubcategorias(sub_id, sub_nome),
            parse_mode="HTML"
        )
        return ST_SUBSUB

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
    context.user_data["subsub_id"] = None
    context.user_data["subsub_nome"] = None

    botoes = [
        [InlineKeyboardButton("➕ Criar sub-subcategoria (grupo)", callback_data="wiz_subsub_sim")],
        [InlineKeyboardButton("➡️ Sem grupo (ir para nomes)", callback_data="wiz_subsub_nao")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="wiz_cancel")],
    ]
    await update.message.reply_text(
        f"✅ Subcategoria <b>{esc(sub_oficial)}</b> pronta!\n\nEssa subcategoria vai ter sub-subcategorias (grupos)?",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )
    return ST_ASK_SUBSUB_CHOICE

async def wiz_decidir_subsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ST_ASK_SUBSUB_CHOICE

    await query.answer()
    if query.data == "wiz_subsub_sim":
        await query.edit_message_text("✏️ Digite o <b>nome da nova sub-subcategoria (grupo)</b>:", parse_mode="HTML")
        return ST_NEW_SUBSUB

    await query.edit_message_text(
        f"📂 <b>{esc(context.user_data['cat_nome'])} • {esc(context.user_data['sub_nome'])}</b>\n\n"
        f"<b>Passo 3/5</b> — Selecione um nome ou clique em <b>Criar novo nome</b>:",
        reply_markup=montar_teclado_nomes(context),
        parse_mode="HTML"
    )
    return ST_NOME

async def wiz_escolher_subsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ST_SUBSUB

    await query.answer()
    if query.data == "wiz_new_subsub":
        await query.edit_message_text("✏️ Digite o <b>nome da nova sub-subcategoria (grupo)</b>:", parse_mode="HTML")
        return ST_NEW_SUBSUB

    if query.data == "wiz_no_subsub":
        context.user_data["subsub_id"] = None
        context.user_data["subsub_nome"] = None
        await query.edit_message_text(
            f"📂 <b>{esc(context.user_data['cat_nome'])} • {esc(context.user_data['sub_nome'])}</b>\n\n"
            f"<b>Passo 3/5</b> — Selecione um artista/nome ou clique em <b>Criar novo nome</b>:",
            reply_markup=montar_teclado_nomes(context),
            parse_mode="HTML"
        )
        return ST_NOME

    subsub_id = int(query.data.split("|")[1])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM subsubcategorias WHERE id = %s", (subsub_id,))
    subsub_nome = cur.fetchone()[0]
    cur.close()
    conn.close()

    context.user_data["subsub_id"] = subsub_id
    context.user_data["subsub_nome"] = subsub_nome

    await query.edit_message_text(
        f"📂 <b>{esc(context.user_data['cat_nome'])} • {esc(context.user_data['sub_nome'])} • {esc(subsub_nome)}</b>\n\n"
        f"<b>Passo 3/5</b> — Selecione um integrante/artista ou clique em <b>Criar novo nome</b>:",
        reply_markup=montar_teclado_nomes(context),
        parse_mode="HTML"
    )
    return ST_NOME

async def wiz_receber_nova_subsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ST_NEW_SUBSUB

    subsub_nome = update.message.text.strip()
    sub_id = context.user_data["sub_id"]

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, nome FROM subsubcategorias WHERE subcategoria_id = %s AND LOWER(nome) = LOWER(%s)",
        (sub_id, subsub_nome)
    )
    existente = cur.fetchone()
    if existente:
        subsub_id, subsub_oficial = existente
    else:
        cur.execute(
            "INSERT INTO subsubcategorias (subcategoria_id, nome) VALUES (%s, %s) RETURNING id",
            (sub_id, subsub_nome)
        )
        subsub_id = cur.fetchone()[0]
        subsub_oficial = subsub_nome
        conn.commit()
    cur.close()
    conn.close()

    context.user_data["subsub_id"] = subsub_id
    context.user_data["subsub_nome"] = subsub_oficial

    await update.message.reply_text(
        f"✅ Sub-subcategoria <b>{esc(subsub_oficial)}</b> pronta!\n\n"
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

    if query.data == "wiz_new_subsub_from_nome":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>nome da nova sub-subcategoria (grupo)</b>:", parse_mode="HTML")
        return ST_NEW_SUBSUB

    if query.data == "wiz_new_nome":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>nome do novo artista / integrante / personagem</b>:", parse_mode="HTML")
        return ST_NEW_NOME

    idx = int(query.data.split("|")[1])
    nomes_lista = context.user_data.get("nomes_lista", [])
    if idx >= len(nomes_lista):
        await query.answer("Lista expirada, tente novamente.", show_alert=True)
        return ST_NOME

    nome_escolhido = nomes_lista[idx]

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM cartas
        WHERE LOWER(nome) = LOWER(%s) AND event_id IS NOT DISTINCT FROM %s AND categoria_id = %s
          AND subcategoria_id IS NOT DISTINCT FROM %s
          AND subsubcategoria_id IS NOT DISTINCT FROM %s
    """, (nome_escolhido, context.user_data.get("event_id"), context.user_data["cat_id"], context.user_data.get("sub_id"), context.user_data.get("subsub_id")))
    total_tiers = cur.fetchone()[0]
    modo_tiers = categoria_usa_tiers(context.user_data["cat_id"])
    cur.close()
    conn.close()

    if (modo_tiers and total_tiers >= 5) or (not modo_tiers and total_tiers >= 1):
        msg = "já está completo (5/5 tiers)!" if modo_tiers else "já tem sua carta única cadastrada nessa categoria!"
        await query.answer(f"{nome_escolhido} {msg}", show_alert=True)
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
    cur.execute("SELECT nome FROM cartas WHERE LOWER(nome)=LOWER(%s) AND categoria_id=%s AND subcategoria_id IS NOT DISTINCT FROM %s AND subsubcategoria_id IS NOT DISTINCT FROM %s AND event_id IS NOT DISTINCT FROM %s LIMIT 1", (novo_nome,context.user_data["cat_id"],context.user_data.get("sub_id"),context.user_data.get("subsub_id"),context.user_data.get("event_id")))
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

    if context.user_data.get("event_id"):
        conn=get_db(); cur=conn.cursor()
        cur.execute("SELECT id FROM cartas WHERE LOWER(nome)=LOWER(%s) AND event_id=%s AND categoria_id=%s AND subcategoria_id IS NOT DISTINCT FROM %s AND subsubcategoria_id IS NOT DISTINCT FROM %s LIMIT 1",(context.user_data.get("nome_carta"),context.user_data["event_id"],context.user_data["cat_id"],context.user_data.get("sub_id"),context.user_data.get("subsub_id")))
        duplicate=cur.fetchone(); cur.close(); conn.close()
        if duplicate:
            await msg.reply_text(f"⚠️ Já existe uma carta <b>{esc(context.user_data.get('nome_carta'))}</b> nessa categoria do evento (#{duplicate[0]}).",parse_mode="HTML")
            return ST_MIDIA
        await msg.reply_text(
            "🎉 Carta de evento: ela não usa tiers normais. Confirme o cadastro:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Cadastrar carta do evento", callback_data="wiz_tier|1")], [InlineKeyboardButton("🔙 Voltar ao Studio", callback_data="wiz_cancel")]]),
        )
        return ST_TIER

    nome_carta = context.user_data["nome_carta"]
    conn = get_db()
    cur = conn.cursor()
    cat_id = context.user_data["cat_id"]
    cur.execute("SELECT tier FROM cartas WHERE LOWER(nome) = LOWER(%s) AND event_id IS NULL AND categoria_id = %s AND subcategoria_id IS NOT DISTINCT FROM %s AND subsubcategoria_id IS NOT DISTINCT FROM %s", (nome_carta, cat_id, context.user_data.get("sub_id"), context.user_data.get("subsub_id")))
    ocupados = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.close()

    botoes = []
    tiers_disponiveis = range(1, 7) if categoria_usa_tiers(cat_id) else range(1, 2)
    for t in tiers_disponiveis:
        t_cfg = TIERS_CONFIG[t]
        em_btn = texto_limpo_botao(obter_emoji_tier(t))
        if t in ocupados:
            botoes.append([InlineKeyboardButton(f"⛔ {t_cfg['nome']} (Já existe)", callback_data=f"wiz_tier_lock|{t}")])
        else:
            botoes.append([InlineKeyboardButton(f"{em_btn} {t_cfg['nome']}", callback_data=f"wiz_tier|{t}")])
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
    event_id = context.user_data.get("event_id")
    if event_id:
        tier = 1
    if not categoria_usa_tiers(cat_id) and tier != 1:
        await query.answer("Essa categoria usa tier único.", show_alert=True)
        return ST_TIER
    cat_nome = context.user_data["cat_nome"]
    sub_id = context.user_data.get("sub_id")
    sub_nome = context.user_data.get("sub_nome")
    subsub_id = context.user_data.get("subsub_id")
    subsub_nome = context.user_data.get("subsub_nome")
    raw_file_id = context.user_data["raw_file_id"]
    media_type = context.user_data["media_type"]

    await query.answer("Gerando carta...")
    await query.edit_message_text("⏳ Padronizando carta e finalizando cadastro...")

    if media_type == "photo":
        tg_file = await context.bot.get_file(raw_file_id)
        buf_in = io.BytesIO()
        await tg_file.download_to_memory(buf_in)
        if event_id:
            conn_frame = get_db(); cur_frame = conn_frame.cursor()
            cur_frame.execute("SELECT moldura_png FROM eventos WHERE id=%s", (event_id,)); moldura_evento = cur_frame.fetchone()
            cur_frame.close(); conn_frame.close()
            buf_out = await asyncio.to_thread(_compor_imagem_com_moldura, buf_in.getvalue(), moldura_evento[0] if moldura_evento else None)
        else:
            buf_out = await asyncio.to_thread(processar_imagem_carta, buf_in.getvalue(), tier)

        sent_msg = await query.message.reply_photo(
            photo=buf_out,
            caption=f"🖼️ <i>Prévia final ({LARGURA_CARTA}×{ALTURA_CARTA})</i>",
            parse_mode="HTML"
        )
        final_file_id = sent_msg.photo[-1].file_id
    else:
        final_file_id = raw_file_id

    peso = TIERS_CONFIG[tier]["peso"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO cartas (nome, categoria_id, subcategoria_id, subsubcategoria_id, tier, peso_drop, file_id, media_type, original_file_id, event_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (nome_carta, cat_id, sub_id, subsub_id, tier, peso, final_file_id, media_type, raw_file_id, event_id))
    nova_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    t_cfg = TIERS_CONFIG[tier]
    t_em = obter_emoji_tier(tier)
    cat_codigo, local_str = formatar_local_carta(cat_id, cat_nome, sub_id, sub_nome, subsub_id, subsub_nome)
    botoes_final = []
    if not event_id:
        botoes_final.append([InlineKeyboardButton("➕ Adicionar outro tier deste personagem", callback_data=f"studio_go|addtier|{nova_id}")])
    botoes_final.extend([
        [InlineKeyboardButton("➕ Cadastrar outra carta", callback_data="studio_go|addcarta")],
        [InlineKeyboardButton("🛠️ Voltar ao Studio", callback_data="studio_menu")],
    ])
    await query.message.reply_text(
        f"✅ <b>Carta cadastrada com sucesso!</b>\n\n" +
        (f"🎉 <b>{nova_id}. {esc(nome_carta)}</b> • carta de evento\n" if event_id else f"{t_em} <b>{nova_id}. {esc(nome_carta)}.</b> <i>[{t_cfg['curto']}]</i>\n") +
        f"💿 <b>{cat_codigo}. {local_str}</b>",
        reply_markup=InlineKeyboardMarkup(botoes_final),
        parse_mode="HTML"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Nova carta", callback_data="studio_go|addcarta"), InlineKeyboardButton("🛠️ Painel Studio", callback_data="studio_menu")],
        [InlineKeyboardButton("✏️ Editar cartas", callback_data="studio_go|editcarta"), InlineKeyboardButton("📂 Categorias", callback_data="studio_go|categorias")],
    ])
    if update.callback_query:
        await update.callback_query.answer("Cancelado.")
        await update.callback_query.edit_message_text("Cadastro cancelado. O que deseja fazer agora?", reply_markup=teclado)
    else:
        await update.message.reply_text("Cadastro cancelado. O que deseja fazer agora?", reply_markup=teclado)
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# ASSISTENTE INTERATIVO /editcarta (COM PAGINAÇÃO & 3 NÍVEIS)
# ==========================================
def _carregar_carta_dict(carta_id: int) -> dict | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome, c.categoria_id, cat.nome, c.subcategoria_id, sub.nome,
               c.subsubcategoria_id, subsub.nome, c.tier, c.file_id, c.media_type
        FROM cartas c
        JOIN categorias cat ON c.categoria_id = cat.id
        LEFT JOIN subcategorias sub ON c.subcategoria_id = sub.id
        LEFT JOIN subsubcategorias subsub ON c.subsubcategoria_id = subsub.id
        WHERE c.id = %s
    """, (carta_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "nome": row[1],
        "cat_id": row[2],
        "cat_nome": row[3],
        "sub_id": row[4],
        "sub_nome": row[5],
        "subsub_id": row[6],
        "subsub_nome": row[7],
        "tier": row[8],
        "file_id": row[9],
        "media_type": row[10],
    }

def _montar_lista_selecao_edicao(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cartas")
    total = cur.fetchone()[0]

    total_paginas = max(1, (total + ED_PAGE_SIZE - 1) // ED_PAGE_SIZE)
    page = max(0, min(page, total_paginas - 1))
    offset = page * ED_PAGE_SIZE

    cur.execute(
        "SELECT id, nome, tier FROM cartas ORDER BY id DESC LIMIT %s OFFSET %s",
        (ED_PAGE_SIZE, offset)
    )
    cartas_pag = cur.fetchall()
    cur.close()
    conn.close()

    botoes = []
    linha = []
    for cid, cnome, ctier in cartas_pag:
        emoji = texto_limpo_botao(obter_emoji_tier(ctier))
        linha.append(InlineKeyboardButton(f"#{cid} {cnome} {emoji}", callback_data=f"ed_pick|{cid}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    if total_paginas > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"ed_page|{page - 1}"))
        nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_paginas}", callback_data="ed_noop"))
        if page + 1 < total_paginas:
            nav.append(InlineKeyboardButton("Próxima ➡️", callback_data=f"ed_page|{page + 1}"))
        botoes.append(nav)

    botoes.append([InlineKeyboardButton("🔍 Pesquisar carta por nome", callback_data="ed_search")])
    botoes.append([InlineKeyboardButton("🛠️ Voltar ao Studio", callback_data="studio_menu")])

    texto = (
        f"🛠️ <b>Editar Carta</b> <i>(Página {page + 1}/{total_paginas} • {total} cartas)</i>\n\n"
        f"Escolha uma carta nos botões ou digite o <b>ID da carta</b>:"
    )
    return texto, InlineKeyboardMarkup(botoes)

def _montar_resultados_edicao(busca: str, page: int = 0):
    por_pagina = 10
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM cartas WHERE nome ILIKE %s", (f"%{busca}%",))
    total = cur.fetchone()[0]
    paginas = max(1, (total + por_pagina - 1) // por_pagina)
    page = max(0, min(page, paginas - 1))
    cur.execute("SELECT id, nome, tier FROM cartas WHERE nome ILIKE %s ORDER BY nome, tier, id LIMIT %s OFFSET %s", (f"%{busca}%", por_pagina, page * por_pagina))
    cartas = cur.fetchall()
    cur.close()
    conn.close()
    botoes = []
    linha = []
    for cid, nome, tier in cartas:
        linha.append(InlineKeyboardButton(f"#{cid} {nome[:28]} T{tier}", callback_data=f"ed_pick|{cid}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    if paginas > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"ed_searchpage|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{paginas}", callback_data="ed_noop"))
        if page + 1 < paginas:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"ed_searchpage|{page + 1}"))
        botoes.append(nav)
    botoes.append([InlineKeyboardButton("🔍 Nova pesquisa", callback_data="ed_search"), InlineKeyboardButton("🔙 Lista completa", callback_data="ed_back_list")])
    return f"Cartas encontradas para <b>{esc(busca)}</b> ({total}):", InlineKeyboardMarkup(botoes)

def _montar_menu_edicao(d: dict) -> tuple[str, InlineKeyboardMarkup]:
    t_cfg = TIERS_CONFIG[d["tier"]]
    t_em = obter_emoji_tier(d["tier"])
    cat_codigo, local_str = formatar_local_carta(
        d["cat_id"], d["cat_nome"], d["sub_id"], d["sub_nome"], d.get("subsub_id"), d.get("subsub_nome")
    )
    midia_label = "GIF" if d["media_type"] == "animation" else f"Foto ({LARGURA_CARTA}×{ALTURA_CARTA})"

    texto = (
        f"🛠️ <b>Editando Carta #{d['id']}</b>\n\n"
        f"{t_em} <b>Nome:</b> {esc(d['nome'])}\n"
        f"💿 <b>Categoria:</b> {cat_codigo}. {local_str}\n"
        f"💎 <b>Tier:</b> {t_cfg['nome']}\n"
        f"🖼️ <b>Mídia:</b> {midia_label}\n\n"
        f"Escolha o que deseja alterar:"
    )

    botoes = [
        [InlineKeyboardButton("🏷️ Editar tags", callback_data="ed_tags")],
        [
            InlineKeyboardButton("✏️ Nome", callback_data="ed_op|nome"),
            InlineKeyboardButton("📂 Categoria/Sub", callback_data="ed_op|cat"),
        ],
        [
            InlineKeyboardButton("🖼️ Foto / GIF", callback_data="ed_op|midia"),
            InlineKeyboardButton("💎 Tier", callback_data="ed_op|tier"),
        ],
        [
            InlineKeyboardButton("✅ Salvar alterações", callback_data="ed_salvar"),
            InlineKeyboardButton("🔙 Lista de Cartas", callback_data="ed_back_list"),
        ],
        [InlineKeyboardButton("🗑️ Excluir esta carta", callback_data="ed_delete")],
        [InlineKeyboardButton("🛠️ Voltar ao Studio", callback_data="studio_menu")],
        [
            InlineKeyboardButton("❌ Cancelar", callback_data="ed_cancel"),
        ],
    ]
    return texto, InlineKeyboardMarkup(botoes)

async def ed_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update.effective_user.id):
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["admin_id"] = update.effective_user.id
    context.user_data["ed_page"] = 0

    if not update.callback_query and context.args:
        alvo_raw = context.args[0].replace("#", "").strip()
        if alvo_raw.isdigit():
            d = _carregar_carta_dict(int(alvo_raw))
            if not d:
                await update.effective_message.reply_text("Carta não encontrada com esse ID.")
                return ConversationHandler.END
            context.user_data["ed_carta"] = d
            texto, markup = _montar_menu_edicao(d)
            await update.effective_message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
            return ED_MENU

    texto, markup = _montar_lista_selecao_edicao(0)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_receber_id_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ED_MENU

    if context.user_data.pop("ed_waiting_tags", False):
        d=context.user_data.get("ed_carta")
        if not d: return ED_MENU
        values=[]
        for item in update.message.text.split(","):
            name=item.strip().lstrip("#")[:24]
            if name and name.lower() not in {v.lower() for v in values}: values.append(name)
        if len(values)>3:
            context.user_data["ed_waiting_tags"]=True
            await update.message.reply_text("Cada carta pode ter até 3 tags. Envie no máximo três, separadas por vírgula:")
            return ED_MENU
        conn=get_db(); cur=conn.cursor()
        cur.execute("DELETE FROM carta_tags WHERE carta_id=%s",(d["id"],))
        for value in values:
            cur.execute("SELECT id FROM tags WHERE LOWER(nome)=LOWER(%s)",(value,)); row=cur.fetchone()
            if row: tid=row[0]
            else:
                cur.execute("INSERT INTO tags(nome) VALUES(%s) RETURNING id",(value,)); tid=cur.fetchone()[0]
            cur.execute("INSERT INTO carta_tags(carta_id,tag_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(d["id"],tid))
        conn.commit(); cur.close(); conn.close()
        texto,markup=_montar_menu_edicao(d)
        await update.message.reply_text("Tags atualizadas.\n\n"+texto,reply_markup=markup,parse_mode="HTML")
        return ED_MENU

    alvo_raw = update.message.text.replace("#", "").strip()
    if not alvo_raw.isdigit():
        busca = alvo_raw.strip()
        if not busca:
            await update.message.reply_text("Digite o nome da carta que deseja localizar.")
            return ED_MENU
        context.user_data["ed_search_query"] = busca
        texto, markup = _montar_resultados_edicao(busca)
        await update.message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    d = _carregar_carta_dict(int(alvo_raw))
    if not d:
        await update.message.reply_text("⚠️ Carta não encontrada. Tente outro ID:")
        return ED_MENU

    context.user_data["ed_carta"] = d
    texto, markup = _montar_menu_edicao(d)
    await update.message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ED_MENU

    if query.data == "ed_noop":
        await query.answer()
        return ED_MENU

    if query.data == "ed_tags":
        d=context.user_data.get("ed_carta")
        if not d:
            await query.answer("Escolha uma carta primeiro.",show_alert=True); return ED_MENU
        conn=get_db(); cur=conn.cursor(); cur.execute("SELECT t.nome FROM tags t JOIN carta_tags ct ON ct.tag_id=t.id WHERE ct.carta_id=%s ORDER BY t.nome",(d["id"],)); current=", ".join(x[0] for x in cur.fetchall()); cur.close(); conn.close()
        context.user_data["ed_waiting_tags"]=True
        await query.answer(); await query.edit_message_text(f"Tags atuais: {esc(current) if current else 'nenhuma'}\nEnvie até 3 tags separadas por vírgula (até 24 caracteres cada). Envie uma mensagem vazia para remover todas:")
        return ED_MENU

    if query.data == "studio_menu":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Nova carta", callback_data="studio_go|addcarta"), InlineKeyboardButton("✏️ Editar cartas", callback_data="studio_go|editcarta")],
            [InlineKeyboardButton("📂 Categorias & grupos", callback_data="studio_go|categorias"), InlineKeyboardButton("📝 Textos públicos", callback_data="studio_go|textos")],
            [InlineKeyboardButton("📚 Catálogo", callback_data="studio_go|catalogo"), InlineKeyboardButton("📦 Mover artista", callback_data="studio_go|moverartista")],
        ])
        await query.answer()
        await query.edit_message_text("🛠️ <b>Ecco-Mix Studio</b>\n\nEscolha o que deseja fazer:", reply_markup=teclado, parse_mode="HTML")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data.startswith("ed_page|"):
        nova_pag = int(query.data.split("|")[1])
        context.user_data["ed_page"] = nova_pag
        await query.answer()
        texto, markup = _montar_lista_selecao_edicao(nova_pag)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    if query.data == "ed_back_list":
        await query.answer()
        pag = context.user_data.get("ed_page", 0)
        texto, markup = _montar_lista_selecao_edicao(pag)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    if query.data == "ed_search":
        await query.answer()
        await query.edit_message_text("Digite o nome completo ou um trecho do nome da carta:")
        return ED_MENU

    if query.data.startswith("ed_searchpage|"):
        page = int(query.data.split("|")[1])
        busca = context.user_data.get("ed_search_query", "")
        texto, markup = _montar_resultados_edicao(busca, page)
        await query.answer()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    if query.data.startswith("ed_pick|"):
        cid = int(query.data.split("|")[1])
        d = _carregar_carta_dict(cid)
        if not d:
            await query.answer("Carta não encontrada.", show_alert=True)
            return ED_MENU
        await query.answer()
        context.user_data["ed_carta"] = d
        texto, markup = _montar_menu_edicao(d)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    if query.data == "ed_delete":
        d = context.user_data.get("ed_carta")
        if not d:
            await query.answer("Escolha uma carta primeiro.", show_alert=True)
            return ED_MENU
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Confirmar exclusão", callback_data="ed_delete_confirm"), InlineKeyboardButton("Cancelar", callback_data="ed_delete_cancel")],
        ])
        await query.answer()
        await query.edit_message_text(f"Excluir permanentemente <b>#{d['id']} {esc(d['nome'])}</b>? A carta também será removida das coleções e wishlists.", reply_markup=teclado, parse_mode="HTML")
        return ED_MENU

    if query.data == "ed_delete_cancel":
        d = context.user_data.get("ed_carta")
        await query.answer("Exclusão cancelada.")
        texto, markup = _montar_menu_edicao(d)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    if query.data == "ed_delete_confirm":
        d = context.user_data.get("ed_carta")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM trocas WHERE status = 'pendente' AND (carta_oferecida_id = %s OR carta_pedida_id = %s)", (d["id"], d["id"]))
        cur.execute("DELETE FROM cartas WHERE id = %s", (d["id"],))
        conn.commit()
        cur.close()
        conn.close()
        context.user_data.pop("ed_carta", None)
        texto, markup = _montar_lista_selecao_edicao(context.user_data.get("ed_page", 0))
        await query.answer("Carta excluída.")
        await query.edit_message_text("Carta excluída.\n\n" + texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    d = context.user_data.get("ed_carta")
    if not d:
        await query.answer("Sessão expirada. Use /editcarta novamente.", show_alert=True)
        return ConversationHandler.END

    if query.data == "ed_salvar":
        conn = get_db()
        cur = conn.cursor()
        if not categoria_usa_tiers(d["cat_id"]):
            d["tier"] = 1
        cur.execute("SELECT id FROM cartas WHERE LOWER(nome) = LOWER(%s) AND categoria_id = %s AND tier = %s AND id != %s", (d["nome"], d["cat_id"], d["tier"], d["id"]))
        conflito = cur.fetchone()
        if conflito:
            cur.close()
            conn.close()
            await query.answer(f"Já existe outra carta de {d['nome']} no Tier {d['tier']} (#{conflito[0]})!", show_alert=True)
            return ED_MENU

        peso = TIERS_CONFIG[d["tier"]]["peso"]
        cur.execute("""
            UPDATE cartas
            SET nome = %s, categoria_id = %s, subcategoria_id = %s, subsubcategoria_id = %s,
                tier = %s, peso_drop = %s, file_id = %s, media_type = %s
            WHERE id = %s
        """, (d["nome"], d["cat_id"], d["sub_id"], d.get("subsub_id"), d["tier"], peso, d["file_id"], d["media_type"], d["id"]))
        conn.commit()
        cur.close()
        conn.close()

        await query.answer("Salvo!")
        t_cfg = TIERS_CONFIG[d["tier"]]
        t_em = obter_emoji_tier(d["tier"])
        cat_codigo, local_str = formatar_local_carta(
            d["cat_id"], d["cat_nome"], d["sub_id"], d["sub_nome"], d.get("subsub_id"), d.get("subsub_nome")
        )
        await query.edit_message_text(
            f"✅ <b>Carta #{d['id']} atualizada!</b>\n\n"
            f"{t_em} <b>{d['id']}. {esc(d['nome'])}.</b> <i>[{t_cfg['curto']}]</i>\n"
            f"💿 <b>{cat_codigo}. {local_str}</b>\n\nO que deseja fazer agora?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Editar outra carta", callback_data="studio_go|editcarta")],
                [InlineKeyboardButton("🛠️ Voltar ao Studio", callback_data="studio_menu")],
            ]),
            parse_mode="HTML"
        )
        context.user_data.clear()
        return ConversationHandler.END

    _, op = query.data.split("|")
    await query.answer()

    if op == "nome":
        await query.edit_message_text(
            f"✏️ Nome atual: <b>{esc(d['nome'])}</b>\n\nDigite o <b>novo nome</b> da carta:",
            parse_mode="HTML"
        )
        return ED_NOME

    if op == "cat":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, nome FROM categorias ORDER BY id ASC")
        cats = cur.fetchall()
        cur.close()
        conn.close()

        botoes = []
        linha = []
        for cid, cnome in cats:
            linha.append(InlineKeyboardButton(cnome, callback_data=f"ed_cat|{cid}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
        botoes.append([InlineKeyboardButton("➕ Criar nova categoria", callback_data="ed_new_cat")])
        botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="ed_voltar")])

        await query.edit_message_text(
            "📂 Escolha a <b>nova categoria</b> ou crie uma:",
            reply_markup=InlineKeyboardMarkup(botoes),
            parse_mode="HTML"
        )
        return ED_CAT

    if op == "midia":
        await query.edit_message_text(
            f"🖼️ Envie a <b>nova foto</b> (padronizada em {LARGURA_CARTA}×{ALTURA_CARTA}) ou <b>novo GIF</b> para a carta <b>#{d['id']}</b>:",
            parse_mode="HTML"
        )
        return ED_MIDIA

    if op == "tier":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT tier FROM cartas WHERE LOWER(nome) = LOWER(%s) AND categoria_id = %s AND id != %s", (d["nome"], d["cat_id"], d["id"]))
        ocupados = {r[0] for r in cur.fetchall()}
        cur.close()
        conn.close()

        botoes = []
        tiers_disponiveis = range(1, 7) if categoria_usa_tiers(d["cat_id"]) else range(1, 2)
        for t in tiers_disponiveis:
            t_cfg = TIERS_CONFIG[t]
            em_btn = texto_limpo_botao(obter_emoji_tier(t))
            if t in ocupados:
                botoes.append([InlineKeyboardButton(f"⛔ {t_cfg['nome']} (Já existe)", callback_data=f"ed_tlock|{t}")])
            else:
                marca = " (Atual)" if t == d["tier"] else ""
                botoes.append([InlineKeyboardButton(f"{em_btn} {t_cfg['nome']}{marca}", callback_data=f"ed_tier|{t}")])
        botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="ed_voltar")])

        await query.edit_message_text(
            f"💎 Escolha o novo <b>Tier</b> para <b>{esc(d['nome'])}</b>:",
            reply_markup=InlineKeyboardMarkup(botoes),
            parse_mode="HTML"
        )
        return ED_TIER

    return ED_MENU

async def ed_receber_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ED_NOME

    novo_nome = update.message.text.strip()
    if not novo_nome:
        await update.message.reply_text("⚠️ Digite um nome válido.")
        return ED_NOME

    d = context.user_data["ed_carta"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM cartas WHERE LOWER(nome) = LOWER(%s) AND categoria_id = %s AND tier = %s AND id != %s", (novo_nome, d["cat_id"], d["tier"], d["id"]))
    conflito = cur.fetchone()
    cur.close()
    conn.close()

    if conflito:
        await update.message.reply_text(
            f"⚠️ Já existe outra carta de <b>{esc(novo_nome)}</b> no Tier {d['tier']} (<code>#{conflito[0]}</code>).\n"
            f"Digite outro nome:",
            parse_mode="HTML"
        )
        return ED_NOME

    d["nome"] = novo_nome
    texto, markup = _montar_menu_edicao(d)
    await update.message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_escolher_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ED_CAT

    d = context.user_data["ed_carta"]
    await query.answer()

    if query.data == "ed_voltar":
        texto, markup = _montar_menu_edicao(d)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    if query.data == "ed_new_cat":
        await query.edit_message_text("✏️ Digite o <b>nome da nova categoria</b>:", parse_mode="HTML")
        return ED_NEW_CAT

    cat_id = int(query.data.split("|")[1])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
    cat_nome = cur.fetchone()[0]
    cur.execute("SELECT id, nome FROM subcategorias WHERE categoria_id = %s ORDER BY id ASC", (cat_id,))
    subs = cur.fetchall()
    cur.close()
    conn.close()

    d["cat_id"] = cat_id
    d["cat_nome"] = cat_nome

    if subs:
        botoes = []
        linha = []
        for sid, snome in subs:
            linha.append(InlineKeyboardButton(snome, callback_data=f"ed_sub|{sid}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
        botoes.append([InlineKeyboardButton("➕ Criar nova subcategoria", callback_data="ed_new_sub")])
        botoes.append([InlineKeyboardButton("➖ Sem subcategoria", callback_data="ed_sem_sub")])

        await query.edit_message_text(
            f"📂 Categoria: <b>{esc(cat_nome)}</b>\nEscolha a <b>subcategoria</b>:",
            reply_markup=InlineKeyboardMarkup(botoes),
            parse_mode="HTML"
        )
        return ED_SUB

    d["sub_id"] = None
    d["sub_nome"] = None
    d["subsub_id"] = None
    d["subsub_nome"] = None
    texto, markup = _montar_menu_edicao(d)
    await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_receber_nova_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ED_NEW_CAT

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

    d = context.user_data["ed_carta"]
    d["cat_id"] = cat_id
    d["cat_nome"] = cat_oficial

    botoes = [
        [InlineKeyboardButton("➕ Criar subcategoria", callback_data="ed_sub_sim")],
        [InlineKeyboardButton("➡️ Sem subcategoria", callback_data="ed_sub_nao")],
    ]
    await update.message.reply_text(
        f"✅ Categoria <b>{esc(cat_oficial)}</b> selecionada. Vai ter subcategoria?",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )
    return ED_ASK_SUB

async def ed_decidir_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ED_ASK_SUB

    await query.answer()
    if query.data == "ed_sub_sim":
        await query.edit_message_text("✏️ Digite o <b>nome da nova subcategoria</b>:", parse_mode="HTML")
        return ED_NEW_SUB

    d = context.user_data["ed_carta"]
    d["sub_id"] = None
    d["sub_nome"] = None
    d["subsub_id"] = None
    d["subsub_nome"] = None
    texto, markup = _montar_menu_edicao(d)
    await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_escolher_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ED_SUB

    d = context.user_data["ed_carta"]
    await query.answer()

    if query.data == "ed_new_sub":
        await query.edit_message_text("✏️ Digite o <b>nome da nova subcategoria</b>:", parse_mode="HTML")
        return ED_NEW_SUB

    if query.data == "ed_sem_sub":
        d["sub_id"] = None
        d["sub_nome"] = None
        d["subsub_id"] = None
        d["subsub_nome"] = None
        texto, markup = _montar_menu_edicao(d)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    sub_id = int(query.data.split("|")[1])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM subcategorias WHERE id = %s", (sub_id,))
    sub_nome = cur.fetchone()[0]
    cur.execute("SELECT id, nome FROM subsubcategorias WHERE subcategoria_id = %s ORDER BY id ASC", (sub_id,))
    subsubs = cur.fetchall()
    cur.close()
    conn.close()

    d["sub_id"] = sub_id
    d["sub_nome"] = sub_nome

    botoes = []
    linha = []
    for ssid, ssnome in subsubs:
        linha.append(InlineKeyboardButton(ssnome, callback_data=f"ed_subsub|{ssid}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    botoes.append([InlineKeyboardButton("➕ Criar sub-subcategoria (grupo)", callback_data="ed_new_subsub")])
    botoes.append([InlineKeyboardButton("➖ Sem sub-subcategoria", callback_data="ed_sem_subsub")])

    await query.edit_message_text(
        f"📂 Subcategoria: <b>{esc(sub_nome)}</b>\nEscolha a <b>sub-subcategoria (grupo)</b> ou deixe sem:",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )
    return ED_SUBSUB

async def ed_receber_nova_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ED_NEW_SUB

    sub_nome = update.message.text.strip()
    d = context.user_data["ed_carta"]
    cat_id = d["cat_id"]

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

    d["sub_id"] = sub_id
    d["sub_nome"] = sub_oficial
    d["subsub_id"] = None
    d["subsub_nome"] = None

    botoes = [
        [InlineKeyboardButton("➕ Criar sub-subcategoria (grupo)", callback_data="ed_subsub_sim")],
        [InlineKeyboardButton("➡️ Sem sub-subcategoria", callback_data="ed_subsub_nao")],
    ]
    await update.message.reply_text(
        f"✅ Subcategoria <b>{esc(sub_oficial)}</b> selecionada. Vai ter sub-subcategoria (grupo)?",
        reply_markup=InlineKeyboardMarkup(botoes),
        parse_mode="HTML"
    )
    return ED_ASK_SUBSUB

async def ed_decidir_subsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ED_ASK_SUBSUB

    await query.answer()
    if query.data == "ed_subsub_sim":
        await query.edit_message_text("✏️ Digite o <b>nome da nova sub-subcategoria (grupo)</b>:", parse_mode="HTML")
        return ED_NEW_SUBSUB

    d = context.user_data["ed_carta"]
    d["subsub_id"] = None
    d["subsub_nome"] = None
    texto, markup = _montar_menu_edicao(d)
    await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_escolher_subsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ED_SUBSUB

    d = context.user_data["ed_carta"]
    await query.answer()

    if query.data == "ed_new_subsub":
        await query.edit_message_text("✏️ Digite o <b>nome da nova sub-subcategoria (grupo)</b>:", parse_mode="HTML")
        return ED_NEW_SUBSUB

    if query.data == "ed_sem_subsub":
        d["subsub_id"] = None
        d["subsub_nome"] = None
        texto, markup = _montar_menu_edicao(d)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    subsub_id = int(query.data.split("|")[1])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM subsubcategorias WHERE id = %s", (subsub_id,))
    subsub_nome = cur.fetchone()[0]
    cur.close()
    conn.close()

    d["subsub_id"] = subsub_id
    d["subsub_nome"] = subsub_nome
    texto, markup = _montar_menu_edicao(d)
    await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_receber_nova_subsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ED_NEW_SUBSUB

    subsub_nome = update.message.text.strip()
    d = context.user_data["ed_carta"]
    sub_id = d["sub_id"]

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, nome FROM subsubcategorias WHERE subcategoria_id = %s AND LOWER(nome) = LOWER(%s)",
        (sub_id, subsub_nome)
    )
    existente = cur.fetchone()
    if existente:
        subsub_id, subsub_oficial = existente
    else:
        cur.execute(
            "INSERT INTO subsubcategorias (subcategoria_id, nome) VALUES (%s, %s) RETURNING id",
            (sub_id, subsub_nome)
        )
        subsub_id = cur.fetchone()[0]
        subsub_oficial = subsub_nome
        conn.commit()
    cur.close()
    conn.close()

    d["subsub_id"] = subsub_id
    d["subsub_nome"] = subsub_oficial
    texto, markup = _montar_menu_edicao(d)
    await update.message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_receber_midia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return ED_MIDIA

    msg = update.message
    d = context.user_data["ed_carta"]

    if msg.animation:
        d["file_id"] = msg.animation.file_id
        d["media_type"] = "animation"
    elif msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/")):
        raw_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
        tg_file = await context.bot.get_file(raw_id)
        buf_in = io.BytesIO()
        await tg_file.download_to_memory(buf_in)
        buf_out = await asyncio.to_thread(processar_imagem_carta, buf_in.getvalue(), d["tier"])
        sent_msg = await msg.reply_photo(
            photo=buf_out,
            caption=f"🖼️ <i>Nova mídia ({LARGURA_CARTA}×{ALTURA_CARTA})</i>",
            parse_mode="HTML"
        )
        d["file_id"] = sent_msg.photo[-1].file_id
        d["media_type"] = "photo"
    else:
        await msg.reply_text("⚠️ Envie uma <b>foto</b> ou <b>GIF</b> válido.", parse_mode="HTML")
        return ED_MIDIA

    texto, markup = _montar_menu_edicao(d)
    await msg.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_escolher_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ED_TIER

    if query.data.startswith("ed_tlock"):
        await query.answer("Esse Tier já existe para esse nome!", show_alert=True)
        return ED_TIER

    d = context.user_data["ed_carta"]
    await query.answer()

    if query.data == "ed_voltar":
        texto, markup = _montar_menu_edicao(d)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return ED_MENU

    novo_tier = int(query.data.split("|")[1])
    if not categoria_usa_tiers(d["cat_id"]) and novo_tier != 1:
        await query.answer("Essa categoria usa tier único.", show_alert=True)
        return ED_TIER
    d["tier"] = novo_tier
    texto, markup = _montar_menu_edicao(d)
    await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    return ED_MENU

async def ed_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Editar cartas", callback_data="studio_go|editcarta"), InlineKeyboardButton("➕ Nova carta", callback_data="studio_go|addcarta")],
        [InlineKeyboardButton("🛠️ Painel Studio", callback_data="studio_menu")],
    ])
    if update.callback_query:
        await update.callback_query.answer("Cancelado.")
        await update.callback_query.edit_message_text("Edição cancelada. O que deseja fazer agora?", reply_markup=teclado)
    else:
        await update.message.reply_text("Edição cancelada. O que deseja fazer agora?", reply_markup=teclado)
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# GERENCIADOR INTERATIVO DE CATEGORIAS (/categorias — 3 NÍVEIS)
# ==========================================
def _montar_home_categorias() -> tuple[str, InlineKeyboardMarkup]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome,
               (SELECT COUNT(*) FROM subcategorias s WHERE s.categoria_id = c.id),
               (SELECT COUNT(*) FROM cartas ct WHERE ct.categoria_id = c.id)
        FROM categorias c
        ORDER BY c.id ASC
    """)
    cats = cur.fetchall()
    cur.close()
    conn.close()

    linhas = ["📂 <b>Gerenciar Categorias, Subcategorias & Grupos</b>\n"]
    botoes = []
    linha = []
    for cid, cnome, qtd_subs, qtd_cartas in cats:
        linhas.append(f"• <code>[{cid}]</code> <b>{esc(cnome)}</b> — {qtd_subs} subs | {qtd_cartas} cartas")
        linha.append(InlineKeyboardButton(f"{cnome}", callback_data=f"catmgr|cat|{cid}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)

    botoes.append([InlineKeyboardButton("🔍 Pesquisar categoria", callback_data="catmgr|search|cat")])
    botoes.append([InlineKeyboardButton("➕ Criar Nova Categoria", callback_data="catmgr|new_cat")])
    botoes.append([InlineKeyboardButton("❌ Fechar", callback_data="catmgr|close")])
    botoes.append([InlineKeyboardButton("🛠️ Voltar ao Studio", callback_data="studio_menu")])
    linhas.append("\nToque em uma categoria para editar ou gerenciar subcategorias/grupos:")
    return "\n".join(linhas), InlineKeyboardMarkup(botoes)

def _montar_view_categoria(cat_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT nome, usa_tiers FROM categorias WHERE id = %s", (cat_id,))
    r_cat = cur.fetchone()
    if not r_cat:
        cur.close()
        conn.close()
        return None
    cat_nome, usa_tiers = r_cat

    cur.execute("SELECT COUNT(*) FROM cartas WHERE categoria_id = %s", (cat_id,))
    total_cartas = cur.fetchone()[0]

    cur.execute("""
        SELECT s.id, s.nome,
               (SELECT COUNT(*) FROM subsubcategorias ss WHERE ss.subcategoria_id = s.id),
               (SELECT COUNT(*) FROM cartas ct WHERE ct.subcategoria_id = s.id)
        FROM subcategorias s
        WHERE s.categoria_id = %s
        ORDER BY s.id ASC
    """, (cat_id,))
    subs = cur.fetchall()
    cur.close()
    conn.close()

    linhas = [
        f"📂 <b>Categoria #{cat_id}: {esc(cat_nome)}</b>",
        f"💿 Cartas vinculadas: <b>{total_cartas}</b>\n",
    ]
    botoes = [
        [InlineKeyboardButton(
            "💎 Tiers: 5 níveis (desativar)" if usa_tiers else "💎 Tiers: unitário (ativar 5 níveis)",
            callback_data="catmgr|toggle_tiers",
        )],
        [
            InlineKeyboardButton("✏️ Renomear Categoria", callback_data="catmgr|ren_cat"),
            InlineKeyboardButton("➕ Nova Subcategoria", callback_data="catmgr|new_sub"),
        ]
    ]

    if subs:
        linhas.append("<b>Subcategorias:</b>")
        linha = []
        for sid, snome, q_grupos, scartas in subs:
            extra_g = f" • {q_grupos} grupos" if q_grupos > 0 else ""
            linhas.append(f"   ↳ <code>[{sid}]</code> {esc(snome)} ({scartas} cartas{extra_g})")
            linha.append(InlineKeyboardButton(f"📑 {snome}", callback_data=f"catmgr|sub|{sid}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
    else:
        linhas.append("<i>Nenhuma subcategoria cadastrada.</i>")

    botoes.append([InlineKeyboardButton("🔍 Pesquisar subcategoria", callback_data="catmgr|search|sub")])
    botoes.append([
        InlineKeyboardButton("🗑️ Excluir Categoria", callback_data="catmgr|del_cat"),
        InlineKeyboardButton("⬅️ Voltar", callback_data="catmgr|home"),
    ])
    return "\n".join(linhas), InlineKeyboardMarkup(botoes)

def consolidar_categoria_em_tier_unico(cur, cat_id: int):
    """Keep one card per character; deleting the others also deletes their inventories."""
    cur.execute(
        "SELECT id, nome, tier FROM cartas WHERE categoria_id = %s ORDER BY LOWER(nome), tier, id",
        (cat_id,),
    )
    grupos = {}
    for carta_id, nome, tier in cur.fetchall():
        grupos.setdefault(nome.strip().casefold(), []).append((carta_id, nome, tier))

    for cartas_nome in grupos.values():
        keeper_id = next((cid for cid, _, tier in cartas_nome if tier == 1), cartas_nome[0][0])
        for carta_id, _, _ in cartas_nome:
            if carta_id == keeper_id:
                continue
            cur.execute("DELETE FROM trocas WHERE status = 'pendente' AND (carta_oferecida_id = %s OR carta_pedida_id = %s)", (carta_id, carta_id))
            cur.execute("DELETE FROM cartas WHERE id = %s", (carta_id,))

        cur.execute(
            "UPDATE cartas SET tier = 1, peso_drop = %s WHERE id = %s",
            (TIERS_CONFIG[1]["peso"], keeper_id),
        )

def _montar_view_subcategoria(sub_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.id, s.nome, s.categoria_id, c.nome,
               (SELECT COUNT(*) FROM cartas ct WHERE ct.subcategoria_id = s.id)
        FROM subcategorias s
        JOIN categorias c ON s.categoria_id = c.id
        WHERE s.id = %s
    """, (sub_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return None

    _, sub_nome, cat_id, cat_nome, qtd_cartas = row

    cur.execute("""
        SELECT ss.id, ss.nome, (SELECT COUNT(*) FROM cartas ct WHERE ct.subsubcategoria_id = ss.id)
        FROM subsubcategorias ss
        WHERE ss.subcategoria_id = %s
        ORDER BY ss.id ASC
    """, (sub_id,))
    subsubs = cur.fetchall()
    cur.close()
    conn.close()

    linhas = [
        f"📑 <b>Subcategoria #{sub_id}: {esc(sub_nome)}</b>",
        f"📂 Categoria pai: <b>{esc(cat_nome)}</b>",
        f"💿 Cartas vinculadas: <b>{qtd_cartas}</b>\n",
    ]

    botoes = [
        [
            InlineKeyboardButton("✏️ Renomear Subcategoria", callback_data="catmgr|ren_sub"),
            InlineKeyboardButton("➕ Novo Grupo (Sub-sub)", callback_data="catmgr|new_subsub"),
        ]
    ]

    if subsubs:
        linhas.append("<b>Sub-subcategorias (Grupos):</b>")
        linha = []
        for ssid, ssnome, sscartas in subsubs:
            linhas.append(f"   ↳ <code>[{ssid}]</code> {esc(ssnome)} ({sscartas} cartas)")
            linha.append(InlineKeyboardButton(f"🏷️ {ssnome}", callback_data=f"catmgr|subsub|{ssid}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
    else:
        linhas.append("<i>Nenhuma sub-subcategoria (grupo) cadastrada.</i>")

    botoes.append([InlineKeyboardButton("🔍 Pesquisar grupo", callback_data="catmgr|search|subsub")])
    botoes.append([
        InlineKeyboardButton("🗑️ Excluir Subcategoria", callback_data="catmgr|del_sub"),
        InlineKeyboardButton("⬅️ Voltar", callback_data=f"catmgr|cat|{cat_id}"),
    ])
    return "\n".join(linhas), InlineKeyboardMarkup(botoes)

def _montar_view_subsubcategoria(subsub_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT ss.id, ss.nome, ss.subcategoria_id, s.nome, c.nome,
               (SELECT COUNT(*) FROM cartas ct WHERE ct.subsubcategoria_id = ss.id)
        FROM subsubcategorias ss
        JOIN subcategorias s ON ss.subcategoria_id = s.id
        JOIN categorias c ON s.categoria_id = c.id
        WHERE ss.id = %s
    """, (subsub_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None

    _, subsub_nome, sub_id, sub_nome, cat_nome, qtd_cartas = row
    texto = (
        f"🏷️ <b>Grupo / Sub-subcategoria #{subsub_id}: {esc(subsub_nome)}</b>\n"
        f"📂 Caminho: <b>{esc(cat_nome)} • {esc(sub_nome)}</b>\n"
        f"💿 Cartas vinculadas: <b>{qtd_cartas}</b>"
    )
    botoes = [
        [
            InlineKeyboardButton("✏️ Renomear Grupo", callback_data="catmgr|ren_subsub"),
            InlineKeyboardButton("🗑️ Excluir Grupo", callback_data="catmgr|del_subsub"),
        ],
        [
            InlineKeyboardButton("⬅️ Voltar", callback_data=f"catmgr|sub|{sub_id}"),
        ],
    ]
    return texto, InlineKeyboardMarkup(botoes)

async def catmgr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update.effective_user.id):
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["admin_id"] = update.effective_user.id

    texto, markup = _montar_home_categorias()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return CM_HOME

async def catmgr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id"):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return CM_HOME

    partes = query.data.split("|")
    acao = partes[1]

    if acao == "studio_menu":
        await studio_menu_callback(update, context)
        context.user_data.clear()
        return ConversationHandler.END

    if acao == "search":
        scope = partes[2]
        context.user_data["cm_search_scope"] = scope
        context.user_data["cm_search_pending"] = True
        await query.answer()
        tipo = {"cat": "categoria", "sub": "subcategoria", "subsub": "grupo"}.get(scope, "opção")
        await query.edit_message_text(f"Digite o nome da {tipo} que deseja localizar:")
        return {"cat": CM_HOME, "sub": CM_CAT_VIEW, "subsub": CM_SUB_VIEW}.get(scope, CM_HOME)

    if acao == "close":
        await query.answer("Fechado.")
        await query.edit_message_text("📂 Gerenciador de categorias fechado.")
        context.user_data.clear()
        return ConversationHandler.END

    if acao == "home":
        await query.answer()
        texto, markup = _montar_home_categorias()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return CM_HOME

    if acao == "new_cat":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>nome da nova categoria</b>:", parse_mode="HTML")
        return CM_NEW_CAT

    if acao == "cat":
        cat_id = int(partes[2])
        res = _montar_view_categoria(cat_id)
        if not res:
            await query.answer("Categoria não encontrada.", show_alert=True)
            return CM_HOME
        await query.answer()
        context.user_data["cm_cat_id"] = cat_id
        await query.edit_message_text(res[0], reply_markup=res[1], parse_mode="HTML")
        return CM_CAT_VIEW

    if acao == "ren_cat":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>novo nome</b> para esta categoria:", parse_mode="HTML")
        return CM_REN_CAT

    if acao == "toggle_tiers":
        cat_id = context.user_data.get("cm_cat_id")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT usa_tiers FROM categorias WHERE id = %s", (cat_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            await query.answer("Categoria não encontrada.", show_alert=True)
            return CM_HOME
        usa_tiers = bool(row[0])
        if usa_tiers:
            cur.close()
            conn.close()
            await query.answer("Confirme a consolidação das cartas.")
            teclado = InlineKeyboardMarkup([
                [InlineKeyboardButton("Confirmar modo unitário", callback_data="catmgr|tiers_confirm")],
                [InlineKeyboardButton("Cancelar", callback_data="catmgr|tiers_cancel")],
            ])
            await query.edit_message_text(
                "⚠️ Ao desativar os tiers, cada personagem ficará com uma carta (priorizando Tier 1). "
                "As cartas duplicadas serão removidas junto com seus dados de coleção, e trocas pendentes ligadas a elas serão canceladas. Continuar?",
                reply_markup=teclado,
            )
            return CM_CAT_VIEW
        cur.execute("UPDATE categorias SET usa_tiers = TRUE WHERE id = %s", (cat_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Cinco tiers ativados.")
        res = _montar_view_categoria(cat_id)
        await query.edit_message_text(res[0], reply_markup=res[1], parse_mode="HTML")
        return CM_CAT_VIEW

    if acao == "tiers_cancel":
        await query.answer("Cancelado.")
        res = _montar_view_categoria(context.user_data.get("cm_cat_id"))
        await query.edit_message_text(res[0], reply_markup=res[1], parse_mode="HTML")
        return CM_CAT_VIEW

    if acao == "tiers_confirm":
        cat_id = context.user_data.get("cm_cat_id")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT usa_tiers FROM categorias WHERE id = %s FOR UPDATE", (cat_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            conn.rollback()
            cur.close()
            conn.close()
            await query.answer("A categoria já está no modo unitário.", show_alert=True)
            return CM_CAT_VIEW
        consolidar_categoria_em_tier_unico(cur, cat_id)
        cur.execute("UPDATE categorias SET usa_tiers = FALSE WHERE id = %s", (cat_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Cartas consolidadas em um tier.")
        res = _montar_view_categoria(cat_id)
        await query.edit_message_text(res[0], reply_markup=res[1], parse_mode="HTML")
        return CM_CAT_VIEW

    if acao == "new_sub":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>nome da nova subcategoria</b>:", parse_mode="HTML")
        return CM_NEW_SUB

    if acao == "del_cat":
        cat_id = context.user_data.get("cm_cat_id")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM cartas WHERE categoria_id = %s", (cat_id,))
        qtd = cur.fetchone()[0]
        if qtd > 0:
            cur.close()
            conn.close()
            await query.answer(f"Não é possível excluir: existem {qtd} cartas nessa categoria!", show_alert=True)
            return CM_CAT_VIEW
        cur.execute("DELETE FROM categorias WHERE id = %s", (cat_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Categoria excluída!")
        texto, markup = _montar_home_categorias()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return CM_HOME

    if acao == "sub":
        sub_id = int(partes[2])
        res = _montar_view_subcategoria(sub_id)
        if not res:
            await query.answer("Subcategoria não encontrada.", show_alert=True)
            return CM_CAT_VIEW
        await query.answer()
        context.user_data["cm_sub_id"] = sub_id
        await query.edit_message_text(res[0], reply_markup=res[1], parse_mode="HTML")
        return CM_SUB_VIEW

    if acao == "ren_sub":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>novo nome</b> para esta subcategoria:", parse_mode="HTML")
        return CM_REN_SUB

    if acao == "new_subsub":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>nome do novo grupo / sub-subcategoria</b>:", parse_mode="HTML")
        return CM_NEW_SUBSUB

    if acao == "del_sub":
        sub_id = context.user_data.get("cm_sub_id")
        cat_id = context.user_data.get("cm_cat_id")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM cartas WHERE subcategoria_id = %s", (sub_id,))
        qtd = cur.fetchone()[0]
        if qtd > 0:
            cur.close()
            conn.close()
            await query.answer(f"Não é possível excluir: existem {qtd} cartas nessa subcategoria!", show_alert=True)
            return CM_SUB_VIEW
        cur.execute("DELETE FROM subcategorias WHERE id = %s", (sub_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Subcategoria excluída!")
        res = _montar_view_categoria(cat_id)
        await query.edit_message_text(res[0], reply_markup=res[1], parse_mode="HTML")
        return CM_CAT_VIEW

    if acao == "subsub":
        subsub_id = int(partes[2])
        res = _montar_view_subsubcategoria(subsub_id)
        if not res:
            await query.answer("Grupo não encontrado.", show_alert=True)
            return CM_SUB_VIEW
        await query.answer()
        context.user_data["cm_subsub_id"] = subsub_id
        await query.edit_message_text(res[0], reply_markup=res[1], parse_mode="HTML")
        return CM_SUBSUB_VIEW

    if acao == "ren_subsub":
        await query.answer()
        await query.edit_message_text("✏️ Digite o <b>novo nome</b> para este grupo / sub-subcategoria:", parse_mode="HTML")
        return CM_REN_SUBSUB

    if acao == "del_subsub":
        subsub_id = context.user_data.get("cm_subsub_id")
        sub_id = context.user_data.get("cm_sub_id")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM cartas WHERE subsubcategoria_id = %s", (subsub_id,))
        qtd = cur.fetchone()[0]
        if qtd > 0:
            cur.close()
            conn.close()
            await query.answer(f"Não é possível excluir: existem {qtd} cartas nesse grupo!", show_alert=True)
            return CM_SUBSUB_VIEW
        cur.execute("DELETE FROM subsubcategorias WHERE id = %s", (subsub_id,))
        conn.commit()
        cur.close()
        conn.close()
        await query.answer("Grupo excluído!")
        res = _montar_view_subcategoria(sub_id)
        await query.edit_message_text(res[0], reply_markup=res[1], parse_mode="HTML")
        return CM_SUB_VIEW

    return CM_HOME

async def catmgr_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("cm_search_pending") or update.effective_user.id != context.user_data.get("admin_id"):
        return CM_HOME
    busca = update.effective_message.text.strip()
    scope = context.user_data.get("cm_search_scope", "cat")
    if scope == "cat":
        sql, params, callback = "SELECT id, nome FROM categorias WHERE nome ILIKE %s ORDER BY nome LIMIT 50", (f"%{busca}%",), "catmgr|cat|{}"
    elif scope == "sub":
        sql, params, callback = "SELECT id, nome FROM subcategorias WHERE categoria_id=%s AND nome ILIKE %s ORDER BY nome LIMIT 50", (context.user_data.get("cm_cat_id"), f"%{busca}%"), "catmgr|sub|{}"
    else:
        sql, params, callback = "SELECT id, nome FROM subsubcategorias WHERE subcategoria_id=%s AND nome ILIKE %s ORDER BY nome LIMIT 50", (context.user_data.get("cm_sub_id"), f"%{busca}%"), "catmgr|subsub|{}"
    conn = get_db()
    cur = conn.cursor()
    cur.execute(sql, params)
    encontrados = cur.fetchall()
    cur.close()
    conn.close()
    botoes = [[InlineKeyboardButton(nome[:55], callback_data=callback.format(item_id))] for item_id, nome in encontrados]
    if not encontrados:
        await update.effective_message.reply_text("Não encontrei opções com esse nome. Tente outra busca.")
        return {"cat": CM_HOME, "sub": CM_CAT_VIEW, "subsub": CM_SUB_VIEW}.get(scope, CM_HOME)
    voltar = "catmgr|home" if scope == "cat" else (f"catmgr|cat|{context.user_data['cm_cat_id']}" if scope == "sub" else f"catmgr|sub|{context.user_data['cm_sub_id']}")
    botoes.append([InlineKeyboardButton("🔍 Pesquisar novamente", callback_data=f"catmgr|search|{scope}"), InlineKeyboardButton("🔙 Voltar", callback_data=voltar)])
    context.user_data["cm_search_pending"] = False
    await update.effective_message.reply_text(f"Resultados para <b>{esc(busca)}</b>:", reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")
    return {"cat": CM_HOME, "sub": CM_CAT_VIEW, "subsub": CM_SUB_VIEW}.get(scope, CM_HOME)

async def catmgr_receber_nova_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return CM_NEW_CAT

    nome = update.message.text.strip()
    if not nome:
        await update.message.reply_text("⚠️ Digite um nome válido.")
        return CM_NEW_CAT

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM categorias WHERE LOWER(nome) = LOWER(%s)", (nome,))
    existente = cur.fetchone()
    if existente:
        cat_id = existente[0]
    else:
        cur.execute("INSERT INTO categorias (nome) VALUES (%s) RETURNING id", (nome,))
        cat_id = cur.fetchone()[0]
        conn.commit()
    cur.close()
    conn.close()

    context.user_data["cm_cat_id"] = cat_id
    res = _montar_view_categoria(cat_id)
    await update.message.reply_text(res[0], reply_markup=res[1], parse_mode="HTML")
    return CM_CAT_VIEW

async def catmgr_receber_ren_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return CM_REN_CAT

    novo_nome = update.message.text.strip()
    cat_id = context.user_data.get("cm_cat_id")
    if not novo_nome:
        await update.message.reply_text("⚠️ Digite um nome válido.")
        return CM_REN_CAT

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM categorias WHERE LOWER(nome) = LOWER(%s) AND id != %s", (novo_nome, cat_id))
    if cur.fetchone():
        cur.close()
        conn.close()
        await update.message.reply_text("⚠️ Já existe outra categoria com esse nome. Digite outro:")
        return CM_REN_CAT

    cur.execute("UPDATE categorias SET nome = %s WHERE id = %s", (novo_nome, cat_id))
    conn.commit()
    cur.close()
    conn.close()

    res = _montar_view_categoria(cat_id)
    await update.message.reply_text(res[0], reply_markup=res[1], parse_mode="HTML")
    return CM_CAT_VIEW

async def catmgr_receber_nova_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return CM_NEW_SUB

    nome = update.message.text.strip()
    cat_id = context.user_data.get("cm_cat_id")
    if not nome:
        await update.message.reply_text("⚠️ Digite um nome válido.")
        return CM_NEW_SUB

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM subcategorias WHERE categoria_id = %s AND LOWER(nome) = LOWER(%s)",
        (cat_id, nome)
    )
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO subcategorias (categoria_id, nome) VALUES (%s, %s)",
            (cat_id, nome)
        )
        conn.commit()
    cur.close()
    conn.close()

    res = _montar_view_categoria(cat_id)
    await update.message.reply_text(res[0], reply_markup=res[1], parse_mode="HTML")
    return CM_CAT_VIEW

async def catmgr_receber_ren_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return CM_REN_SUB

    novo_nome = update.message.text.strip()
    sub_id = context.user_data.get("cm_sub_id")
    cat_id = context.user_data.get("cm_cat_id")
    if not novo_nome:
        await update.message.reply_text("⚠️ Digite um nome válido.")
        return CM_REN_SUB

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM subcategorias WHERE categoria_id = %s AND LOWER(nome) = LOWER(%s) AND id != %s",
        (cat_id, novo_nome, sub_id)
    )
    if cur.fetchone():
        cur.close()
        conn.close()
        await update.message.reply_text("⚠️ Já existe outra subcategoria com esse nome nessa categoria. Digite outro:")
        return CM_REN_SUB

    cur.execute("UPDATE subcategorias SET nome = %s WHERE id = %s", (novo_nome, sub_id))
    conn.commit()
    cur.close()
    conn.close()

    res = _montar_view_subcategoria(sub_id)
    await update.message.reply_text(res[0], reply_markup=res[1], parse_mode="HTML")
    return CM_SUB_VIEW

async def catmgr_receber_nova_subsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return CM_NEW_SUBSUB

    nome = update.message.text.strip()
    sub_id = context.user_data.get("cm_sub_id")
    if not nome:
        await update.message.reply_text("⚠️ Digite um nome válido.")
        return CM_NEW_SUBSUB

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM subsubcategorias WHERE subcategoria_id = %s AND LOWER(nome) = LOWER(%s)",
        (sub_id, nome)
    )
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO subsubcategorias (subcategoria_id, nome) VALUES (%s, %s)",
            (sub_id, nome)
        )
        conn.commit()
    cur.close()
    conn.close()

    res = _montar_view_subcategoria(sub_id)
    await update.message.reply_text(res[0], reply_markup=res[1], parse_mode="HTML")
    return CM_SUB_VIEW

async def catmgr_receber_ren_subsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return CM_REN_SUBSUB

    novo_nome = update.message.text.strip()
    subsub_id = context.user_data.get("cm_subsub_id")
    sub_id = context.user_data.get("cm_sub_id")
    if not novo_nome:
        await update.message.reply_text("⚠️ Digite um nome válido.")
        return CM_REN_SUBSUB

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM subsubcategorias WHERE subcategoria_id = %s AND LOWER(nome) = LOWER(%s) AND id != %s",
        (sub_id, novo_nome, subsub_id)
    )
    if cur.fetchone():
        cur.close()
        conn.close()
        await update.message.reply_text("⚠️ Já existe outro grupo com esse nome nessa subcategoria. Digite outro:")
        return CM_REN_SUBSUB

    cur.execute("UPDATE subsubcategorias SET nome = %s WHERE id = %s", (novo_nome, subsub_id))
    conn.commit()
    cur.close()
    conn.close()

    res = _montar_view_subsubcategoria(subsub_id)
    await update.message.reply_text(res[0], reply_markup=res[1], parse_mode="HTML")
    return CM_SUBSUB_VIEW

async def catmgr_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer("Fechado.")
        await update.callback_query.edit_message_text("📂 Gerenciador de categorias fechado.")
    else:
        await update.message.reply_text("📂 Gerenciador de categorias fechado.")
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# GERENCIADOR DE TEXTOS PÚBLICOS, GIF DO /start E EMOJIS PREMIUM (/textos)
# ==========================================
def _montar_home_textos(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    tem_midia_start = bool(obter_config_valor("start_media_id", ""))
    status_midia = "✅ GIF/Foto ativo" if tem_midia_start else "➖ Sem mídia"
    chaves = list(TEXTOS_META)
    page_size = 8
    total_paginas = max(1, (len(chaves) + page_size - 1) // page_size)
    page = max(0, min(page, total_paginas - 1))

    texto = (
        f"📝 <b>Editor de Textos Públicos</b> — Página {page + 1}/{total_paginas}\n\n"
        "Os textos do jogo, trocas, doações, wishlist, loja e resultados ficam aqui.\n"
        f"🎞️ <b>Mídia do /start:</b> {status_midia}\n\n"
        "💡 <i>Dica: ao enviar qualquer texto ou emoji de Tier, basta usar os <b>Emojis Premium do Telegram</b> normalmente no teclado que o bot salva o ID animado automaticamente!</i>"
    )

    botoes = []
    linha = []
    for chave in chaves[page * page_size:(page + 1) * page_size]:
        linha.append(InlineKeyboardButton(TEXTOS_META[chave]["titulo"][:48], callback_data=f"txmgr|item|{chave}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    if total_paginas > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"txmgr|page|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_paginas}", callback_data="txmgr|noop"))
        if page + 1 < total_paginas:
            nav.append(InlineKeyboardButton("Próxima ➡️", callback_data=f"txmgr|page|{page + 1}"))
        botoes.append(nav)
    botoes.extend([
        [InlineKeyboardButton("🎞️ GIF / Foto do /start", callback_data="txmgr|start_media"), InlineKeyboardButton("🎨 Emojis dos Tiers", callback_data="txmgr|tiers")],
        [InlineKeyboardButton("❌ Fechar", callback_data="txmgr|close")],
    ])
    return texto, InlineKeyboardMarkup(botoes)

def _montar_view_texto(chave: str) -> tuple[str, InlineKeyboardMarkup]:
    meta = TEXTOS_META[chave]
    atual = obter_texto_publico(chave)
    texto = (
        f"📝 <b>{meta['titulo']}</b>\n\n"
        f"<b>Texto atual:</b>\n"
        f"────────────────────\n"
        f"{atual}\n"
        f"────────────────────\n\n"
        f"<b>Variáveis disponíveis:</b>\n<code>{esc(meta['vars'])}</code>"
    )
    botoes = [
        [
            InlineKeyboardButton("✏️ Editar Texto", callback_data="txmgr|edit"),
            InlineKeyboardButton("🔄 Restaurar Padrão", callback_data="txmgr|reset"),
        ],
        [
            InlineKeyboardButton("⬅️ Voltar", callback_data="txmgr|home"),
        ],
    ]
    return texto, InlineKeyboardMarkup(botoes)

def _montar_view_start_media() -> tuple[str, InlineKeyboardMarkup]:
    media_id = obter_config_valor("start_media_id", "")
    media_type = obter_config_valor("start_media_type", "animation")
    status = f"✅ <b>Ativa</b> ({'GIF' if media_type == 'animation' else 'Foto'})" if media_id else "➖ <b>Nenhuma mídia configurada</b>"
    texto = (
        f"🎞️ <b>GIF / Foto do /start</b>\n\n"
        f"Status atual: {status}\n\n"
        f"Envie um <b>GIF</b> ou <b>Foto</b> agora para definir como mídia de boas-vindas do <code>/start</code>:"
    )
    botoes = []
    if media_id:
        botoes.append([InlineKeyboardButton("🗑️ Remover Mídia do /start", callback_data="txmgr|del_start_media")])
    botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="txmgr|home")])
    return texto, InlineKeyboardMarkup(botoes)

def _montar_view_tiers_emojis() -> tuple[str, InlineKeyboardMarkup]:
    linhas = ["🎨 <b>Emojis dos Tiers (Normal ou Premium)</b>\n"]
    botoes = []
    for t in range(1, 7):
        em = obter_emoji_tier(t)
        linhas.append(f"• <b>{TIERS_CONFIG[t]['nome']}:</b> {em}")
        botoes.append([InlineKeyboardButton(f"✏️ Alterar {TIERS_CONFIG[t]['curto']}", callback_data=f"txmgr|tier|{t}")])
    botoes.append([InlineKeyboardButton("⬅️ Voltar", callback_data="txmgr|home")])
    return "\n".join(linhas), InlineKeyboardMarkup(botoes)

async def txmgr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_editor(update.effective_user.id):
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["admin_id"] = update.effective_user.id
    context.user_data["tx_page"] = 0

    texto, markup = _montar_home_textos()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return TX_HOME

async def txmgr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != context.user_data.get("admin_id") or not eh_editor(query.from_user.id):
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return TX_HOME

    partes = query.data.split("|")
    acao = partes[1]

    if acao == "close":
        await query.answer("Fechado.")
        await query.edit_message_text("📝 Editor de textos fechado.")
        context.user_data.clear()
        return ConversationHandler.END

    if acao == "home":
        await query.answer()
        texto, markup = _montar_home_textos(context.user_data.get("tx_page", 0))
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return TX_HOME

    if acao == "page":
        page = int(partes[2])
        context.user_data["tx_page"] = page
        texto, markup = _montar_home_textos(page)
        await query.answer()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return TX_HOME

    if acao == "noop":
        await query.answer()
        return TX_HOME

    if acao == "item":
        chave = partes[2]
        context.user_data["tx_chave"] = chave
        await query.answer()
        texto, markup = _montar_view_texto(chave)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return TX_VIEW

    if acao == "edit":
        chave = context.user_data.get("tx_chave")
        meta = TEXTOS_META[chave]
        await query.answer()
        await query.edit_message_text(
            f"✏️ <b>Editando: {meta['titulo']}</b>\n\n"
            f"Envie agora a <b>nova mensagem</b> (pode usar HTML como <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code> e <b>Emojis Premium do Telegram</b> diretamente):\n\n"
            f"<b>Variáveis:</b> <code>{esc(meta['vars'])}</code>",
            parse_mode="HTML"
        )
        return TX_EDIT_VAL

    if acao == "reset":
        chave = context.user_data.get("tx_chave")
        remover_config_valor(chave)
        await query.answer("Restaurado para o padrão!")
        texto, markup = _montar_view_texto(chave)
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return TX_VIEW

    if acao == "start_media":
        await query.answer()
        texto, markup = _montar_view_start_media()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return TX_START_MEDIA

    if acao == "del_start_media":
        remover_config_valor("start_media_id")
        remover_config_valor("start_media_type")
        await query.answer("Mídia do /start removida!")
        texto, markup = _montar_view_start_media()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return TX_START_MEDIA

    if acao == "tiers":
        await query.answer()
        texto, markup = _montar_view_tiers_emojis()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return TX_TIERS_MENU

    if acao == "tier":
        t = int(partes[2])
        context.user_data["tx_tier"] = t
        await query.answer()
        await query.edit_message_text(
            f"🎨 Envie o <b>novo emoji</b> (normal ou <b>Premium do Telegram</b>) para <b>{TIERS_CONFIG[t]['nome']}</b>:",
            parse_mode="HTML"
        )
        return TX_TIER_EDIT

    return TX_HOME

async def txmgr_receber_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return TX_EDIT_VAL

    chave = context.user_data.get("tx_chave")
    novo_html = extrair_html_com_custom_emojis(update.message).strip()
    if not novo_html:
        await update.message.reply_text("⚠️ Envie um texto válido.")
        return TX_EDIT_VAL

    try:
        salvar_config_valor(chave, novo_html)
        texto, markup = _montar_view_texto(chave)
        await update.message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        remover_config_valor(chave)
        await update.message.reply_text(
            f"⚠️ Erro de formatação HTML no texto enviado ({esc(str(e))}).\n"
            f"Verifique se fechou as tags <code>&lt;b&gt;</code> / <code>&lt;i&gt;</code> corretamente e tente novamente:",
            parse_mode="HTML"
        )
        return TX_EDIT_VAL

    return TX_VIEW

async def txmgr_receber_start_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return TX_START_MEDIA

    msg = update.message
    if msg.animation:
        salvar_config_valor("start_media_id", msg.animation.file_id)
        salvar_config_valor("start_media_type", "animation")
    elif msg.photo:
        salvar_config_valor("start_media_id", msg.photo[-1].file_id)
        salvar_config_valor("start_media_type", "photo")
    else:
        await msg.reply_text("⚠️ Envie um <b>GIF</b> ou <b>Foto</b> válido.", parse_mode="HTML")
        return TX_START_MEDIA

    texto, markup = _montar_view_start_media()
    await msg.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return TX_START_MEDIA

async def txmgr_receber_tier_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != context.user_data.get("admin_id"):
        return TX_TIER_EDIT

    t = context.user_data.get("tx_tier")
    novo_emoji = extrair_html_com_custom_emojis(update.message).strip()
    if not novo_emoji:
        await update.message.reply_text("⚠️ Envie um emoji válido.")
        return TX_TIER_EDIT

    salvar_config_valor(f"tier_emoji_{t}", novo_emoji)
    texto, markup = _montar_view_tiers_emojis()
    await update.message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return TX_TIERS_MENU

async def txmgr_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer("Fechado.")
        await update.callback_query.edit_message_text("📝 Editor de textos fechado.")
    else:
        await update.message.reply_text("📝 Editor de textos fechado.")
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# COMANDOS EXCLUSIVOS DE ADMIN (/studio)
# ==========================================
async def bloqueio_nao_admin(update: Update) -> bool:
    return not eh_admin(update.effective_user.id)

def _studio_teclado(page: int, user_id: int) -> InlineKeyboardMarkup:
    if not eh_admin(user_id):
        return InlineKeyboardMarkup([[InlineKeyboardButton("📝 Textos públicos", callback_data="studio_go|textos")],[InlineKeyboardButton("🏷️ Gerenciar tags",callback_data="studio_ui|tags")]])
    page = max(0, min(2, page))
    botoes = []
    if page == 0:
        botoes = [
            [InlineKeyboardButton("➕ Nova carta", callback_data="studio_go|addcarta"), InlineKeyboardButton("✏️ Editar cartas", callback_data="studio_go|editcarta")],
            [InlineKeyboardButton("🗂️ Categorias", callback_data="studio_go|categorias"), InlineKeyboardButton("📚 Catálogo", callback_data="studio_go|catalogo")],
            [InlineKeyboardButton("📦 Mover artista", callback_data="studio_go|moverartista")],
        ]
    elif page == 1:
        botoes = [
            [InlineKeyboardButton("📝 Textos públicos", callback_data="studio_go|textos")],
            [InlineKeyboardButton("🖼️ Molduras", callback_data="studio_ui|molduras"), InlineKeyboardButton("🎉 Eventos", callback_data="evento|admin")],
        ]
    else:
        botoes = [
            [InlineKeyboardButton("👥 Ver administradores", callback_data="studio_ui|admins|0")],
            [InlineKeyboardButton("🚫 Banimentos", callback_data="studio_ui|ban")],
            [InlineKeyboardButton("🎲 Dar giros", callback_data="studio_ui|help_dargiros"), InlineKeyboardButton("🧹 Zerar inventários", callback_data="studio_ui|help_deleteall")],
        ]
    if page == 1:
        botoes.append([InlineKeyboardButton("🏷️ Gerenciar tags",callback_data="studio_ui|tags")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"studio_ui|page|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/3", callback_data="studio_ui|noop"))
    if page < 2:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"studio_ui|page|{page+1}"))
    botoes.append(nav)
    return InlineKeyboardMarkup(botoes)

async def _mostrar_studio(target, user_id: int, page: int = 0):
    editor_only = not eh_admin(user_id)
    texto = ("🎛️ <b>Ecco-Mix Studio</b>\n\n" if not editor_only else "📝 <b>Editor do Studio</b>\n\n")
    if not editor_only:
        texto += "🎲 <b>Giros:</b> 10 giros diários; reposição à meia-noite (horário de São Paulo). Os giros acumulam.\n\nEscolha uma seção:"
    else:
        texto += "Edite os textos públicos e gerencie tags de artistas/personagens em lote."
    await target.reply_text(texto, reply_markup=_studio_teclado(page, user_id), parse_mode="HTML")

async def studio_ui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not eh_editor(q.from_user.id):
        await q.answer("Apenas equipe autorizada.", show_alert=True); return
    p = q.data.split("|"); action = p[1]
    if action not in ("noop", "page", "tags") and not eh_admin(q.from_user.id):
        await q.answer("Apenas administradores.", show_alert=True); return
    if action == "noop":
        await q.answer(); return
    if action == "page":
        page = int(p[2]); await q.answer()
        if eh_admin(q.from_user.id):
            texto = f"🎛️ <b>Ecco-Mix Studio · Página {page+1}/3</b>\n\n🎲 10 giros diários; reposição à meia-noite de São Paulo; giros acumulam."
            await q.edit_message_text(texto, reply_markup=_studio_teclado(page, q.from_user.id), parse_mode="HTML")
        else:
            await q.edit_message_text("📝 Editor do Studio", reply_markup=_studio_teclado(1, q.from_user.id))
        return
    if action == "molduras":
        if not eh_admin(q.from_user.id):
            await q.answer("Apenas administradores.", show_alert=True); return
        conn=get_db(); cur=conn.cursor(); cur.execute("SELECT tier FROM molduras"); active={r[0] for r in cur.fetchall()}; cur.close(); conn.close()
        rows=[[InlineKeyboardButton(f"{'✅' if t in active else '➕'} {TIERS_CONFIG[t]['nome']}",callback_data=f"studio_ui|molduratri|{t}")] for t in range(1,7)]
        rows.append([InlineKeyboardButton("⬅️ Studio",callback_data="studio_ui|page|1")])
        await q.answer(); await q.edit_message_text("🖼️ <b>Molduras por tier</b>\nEscolha um tier para enviar/substituir a moldura. O arquivo precisa ser PNG.",reply_markup=InlineKeyboardMarkup(rows),parse_mode="HTML"); return
    if action == "tags":
        await q.answer(); await _tagadm_home(q,0); return
    if action == "molduratri":
        tier=int(p[2]); conn=get_db(); cur=conn.cursor(); cur.execute("SELECT 1 FROM molduras WHERE tier=%s",(tier,)); active=cur.fetchone() is not None; cur.close(); conn.close()
        buttons=[[InlineKeyboardButton("📤 Enviar/substituir PNG",callback_data=f"studio_ui|moldurafile|{tier}")]]
        if active: buttons.append([InlineKeyboardButton("🗑 Remover moldura",callback_data=f"studio_ui|molduradel|{tier}")])
        buttons.append([InlineKeyboardButton("⬅️ Voltar",callback_data="studio_ui|molduras")])
        await q.answer(); await q.edit_message_text(f"Moldura de <b>{TIERS_CONFIG[tier]['nome']}</b>",reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML"); return
    if action == "moldurafile":
        tier=int(p[2]); context.user_data["studio_moldura_pendente"] = tier; await q.answer()
        await q.message.reply_text(f"Envie um arquivo PNG para definir a moldura de {TIERS_CONFIG[tier]['nome']}."); return
    if action == "molduradel":
        tier=int(p[2]); conn=get_db(); cur=conn.cursor(); cur.execute("DELETE FROM molduras WHERE tier=%s",(tier,)); conn.commit(); cur.close(); conn.close()
        count=await _regenerar_cartas_moldura_tier(context,tier,None,q.message.chat_id)
        await q.answer("Moldura removida."); await q.message.reply_text(f"Moldura removida; {count} carta(s) restauradas à imagem original."); return
    if action == "admins":
        await _mostrar_lista_admins(q, int(p[2])); return
    if action == "ban":
        if not eh_admin(q.from_user.id):
            await q.answer("Apenas administradores.", show_alert=True); return
        await _mostrar_banimentos(q,0); return
    if action == "bansearch":
        context.user_data["studio_ban_busca"] = True; await q.answer(); await q.message.reply_text("Pesquise pelo @, apelido ou ID do jogador:"); return
    if action == "banpage":
        await _mostrar_banimentos(q,int(p[2])); return
    if action in ("banconfirm","unban"):
        if not eh_admin(q.from_user.id):
            await q.answer("Apenas administradores.", show_alert=True); return
        uid=int(p[2]); conn=get_db(); cur=conn.cursor()
        if action == "banconfirm":
            cur.execute("INSERT INTO banimentos(user_id,aplicado_por) VALUES(%s,%s) ON CONFLICT(user_id) DO NOTHING",(uid,q.from_user.id))
        else:
            cur.execute("DELETE FROM banimentos WHERE user_id=%s",(uid,))
        conn.commit(); cur.close(); conn.close(); await q.answer("Banimento atualizado."); await _mostrar_banimentos(q,0); return
    if action in ("help_dargiros","help_deleteall"):
        command="/dargiros qtd (respondendo à pessoa) ou /dargiros ID qtd" if action=="help_dargiros" else "/deleteall"
        await q.answer(); await q.message.reply_text(f"Use {command}."); return

async def _mostrar_lista_admins(q, page: int = 0):
    if not eh_admin(q.from_user.id):
        await q.answer("Apenas administradores.", show_alert=True); return
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT a.user_id,a.funcao,u.username FROM admins a LEFT JOIN usuarios u ON u.user_id=a.user_id ORDER BY a.funcao,a.user_id")
    rows=cur.fetchall(); cur.close(); conn.close()
    for sid in SUPER_ADMINS:
        if not any(r[0]==sid for r in rows):
            conn=get_db(); cur=conn.cursor(); cur.execute("SELECT username FROM usuarios WHERE user_id=%s",(sid,)); user_row=cur.fetchone(); cur.close(); conn.close()
            rows.insert(0,(sid,"admin",user_row[0] if user_row else None))
    size=10; pages=max(1,(len(rows)+size-1)//size); page=max(0,min(page,pages-1)); chunk=rows[page*size:(page+1)*size]
    lines=["👥 <b>Equipe do bot</b>"]
    for uid,role,username in chunk:
        lines.append(f"• <code>{uid}</code> · {('@'+username) if username else 'sem @'} · {'ADM (todas as permissões)' if role=='admin' else 'Editor (textos públicos e do painel)'}")
    buttons=[]; nav=[]
    if page: nav.append(InlineKeyboardButton("⬅️",callback_data=f"studio_ui|admins|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data="studio_ui|noop"))
    if page+1<pages: nav.append(InlineKeyboardButton("➡️",callback_data=f"studio_ui|admins|{page+1}"))
    buttons.append(nav); buttons.append([InlineKeyboardButton("⬅️ Studio",callback_data="studio_ui|page|2")])
    await q.answer(); await q.edit_message_text("\n".join(lines),reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")

async def _mostrar_banimentos(q, page: int = 0):
    if not eh_admin(q.from_user.id):
        await q.answer("Apenas administradores.", show_alert=True); return
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT b.user_id,u.username,u.apelido FROM banimentos b LEFT JOIN usuarios u ON u.user_id=b.user_id ORDER BY b.criado_em DESC")
    rows=cur.fetchall(); cur.close(); conn.close()
    size=10; pages=max(1,(len(rows)+size-1)//size); page=max(0,min(page,pages-1)); chunk=rows[page*size:(page+1)*size]
    buttons=[[InlineKeyboardButton(f"Desbanir {alias or ('@'+username if username else uid)} · {uid}",callback_data=f"studio_ui|unban|{uid}")] for uid,username,alias in chunk]
    buttons.append([InlineKeyboardButton("🔍 Banir jogador",callback_data="studio_ui|bansearch")])
    nav=[]
    if page: nav.append(InlineKeyboardButton("⬅️",callback_data=f"studio_ui|banpage|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data="studio_ui|noop"))
    if page+1<pages: nav.append(InlineKeyboardButton("➡️",callback_data=f"studio_ui|banpage|{page+1}"))
    buttons.append(nav); buttons.append([InlineKeyboardButton("⬅️ Studio",callback_data="studio_ui|page|2")])
    await q.answer(); await q.edit_message_text("🚫 <b>Banimentos</b>\nEscolha um jogador banido para remover o bloqueio ou pesquise para banir.",reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")

async def handler_banimento_pesquisa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message; uid=update.effective_user.id
    if not context.user_data.get("studio_ban_busca") or not msg or not msg.text: return False
    context.user_data["studio_ban_busca"]=False
    alvo=await _buscar_usuario_perfil(msg.text.strip())
    if not alvo:
        await msg.reply_text("Não encontrei essa pessoa. Ela precisa ter iniciado o bot."); return True
    if eh_admin(alvo):
        await msg.reply_text("Não é possível banir um administrador."); return True
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT username,apelido FROM usuarios WHERE user_id=%s",(alvo,)); row=cur.fetchone(); cur.close(); conn.close()
    nome=(row[1] or ("@"+row[0] if row and row[0] else str(alvo))) if row else str(alvo)
    await msg.reply_text(f"Banir <b>{esc(nome)}</b> (<code>{alvo}</code>) do uso do bot?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Confirmar banimento",callback_data=f"studio_ui|banconfirm|{alvo}")],[InlineKeyboardButton("Cancelar",callback_data="studio_ui|ban")]]),parse_mode="HTML")
    return True

async def handler_usuario_banido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user
    if not user: return
    if eh_admin(user.id): return
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT 1 FROM banimentos WHERE user_id=%s",(user.id,)); banned=cur.fetchone() is not None; cur.close(); conn.close()
    if not banned: return
    if update.callback_query:
        await update.callback_query.answer("Seu acesso ao bot foi bloqueado.",show_alert=True)
    elif update.effective_chat and update.effective_chat.type=="private" and update.effective_message:
        await update.effective_message.reply_text("Seu acesso ao bot foi bloqueado.")
    raise ApplicationHandlerStop

async def handler_studio_moldura_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tier=context.user_data.get("studio_moldura_pendente"); msg=update.effective_message
    if tier is None or not eh_admin(update.effective_user.id): return False
    if not msg or not msg.document or not msg.document.mime_type or "png" not in msg.document.mime_type.lower():
        await msg.reply_text("Envie a moldura como arquivo PNG transparente."); return True
    tg=await context.bot.get_file(msg.document.file_id); buf=io.BytesIO(); await tg.download_to_memory(buf); png=buf.getvalue()
    conn=get_db(); cur=conn.cursor(); cur.execute("INSERT INTO molduras(tier,png_bytes) VALUES(%s,%s) ON CONFLICT(tier) DO UPDATE SET png_bytes=EXCLUDED.png_bytes",(tier,psycopg2.Binary(png))); conn.commit(); cur.close(); conn.close()
    count=await _regenerar_cartas_moldura_tier(context,tier,png,msg.chat_id)
    context.user_data.pop("studio_moldura_pendente",None)
    await msg.reply_text(f"Moldura de {TIERS_CONFIG[tier]['nome']} salva e aplicada em {count} carta(s).")
    return True

async def cmd_studio_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_editor(update.effective_user.id):
        return
    await _mostrar_studio(update.effective_message, update.effective_user.id, 0)
    return
    texto = (
        "🎛️ <b>Ecco-Mix Studio (Painel Admin)</b>\n\n"
        "<b>Criar & Gerenciar Cartas:</b>\n"
        f"• /addcarta — Assistente interativo com botões (Categoria → Subcategoria → Grupo → Nome → Foto {LARGURA_CARTA}×{ALTURA_CARTA} → Tier)\n"
        "• /editcarta (ou <code>/editcarta ID</code>) — Assistente com botões e paginação para editar cartas\n"
        "• /catalogo — Listar cartas por categoria/subcategoria/grupo com paginação\n"
        "• /delcarta <code>ID</code> — Excluir uma carta\n\n"
        "<b>Textos Públicos, GIF do /start & Emojis Premium:</b>\n"
        "• /textos — Editar textos públicos, <code>/comandos</code>, GIF do <code>/start</code> e emojis Premium dos Tiers\n\n"
        "<b>Molduras PNG por Tier (Opcional):</b>\n"
        "• /moldura <code>1 a 5</code> — Responda a um arquivo <b>.PNG transparente</b> para definir a moldura daquele Tier\n"
        "• /molduras — Ver quais Tiers têm moldura ativa\n"
        "• /remmoldura <code>1 a 5</code> — Remover moldura de um Tier\n\n"
        "<b>Categorias, Subcategorias & Grupos:</b>\n"
        "• /categorias — Gerenciar categorias, subcategorias e grupos (sub-subcategorias) por botões\n"
        "• /listacat — Ver lista rápida de IDs e nomes\n"
        "• /editcat <code>ID | NovoNome</code> — Atalho para renomear categoria\n"
        "• /editsub <code>ID | NovoNome</code> — Atalho para renomear subcategoria\n\n"
        "<b>Giros & Equipe:</b>\n"
        "• /dargiros <code>qtd</code> (respondendo a alguém) ou <code>ID qtd</code>\n"
        "• /deleteall — Zerar as cartas obtidas por todos (com confirmação)\n"
        "• /addadm <code>ID</code> (ou responda a alguém) — Adicionar admin\n"
        "• /remadm <code>ID</code> — Remover admin"
    )
    botoes = [
        [
            InlineKeyboardButton("➕ Nova Carta", callback_data="studio_go|addcarta"),
            InlineKeyboardButton("🛠️ Editar Carta", callback_data="studio_go|editcarta"),
        ],
        [
            InlineKeyboardButton("📂 Categorias & Grupos", callback_data="studio_go|categorias"),
            InlineKeyboardButton("📝 Textos & /start", callback_data="studio_go|textos"),
        ],
        [
            InlineKeyboardButton("📜 Catálogo", callback_data="studio_go|catalogo"),
            InlineKeyboardButton("📦 Mover artista/personagem", callback_data="studio_go|moverartista"),
        ],
    ]
    botoes.append([InlineKeyboardButton("🎉 Eventos", callback_data="evento|admin")])
    await update.effective_message.reply_text(texto, reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")

async def studio_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not eh_editor(query.from_user.id):
        await query.answer("Apenas a equipe autorizada pode abrir o Studio.", show_alert=True)
        return
    await query.answer()
    texto_studio = "🎛️ <b>Ecco-Mix Studio</b>\n\n🎲 10 giros diários; reposição à meia-noite de São Paulo; giros acumulam." if eh_admin(query.from_user.id) else "📝 Editor do Studio"
    await query.edit_message_text(texto_studio, reply_markup=_studio_teclado(0, query.from_user.id), parse_mode="HTML")
    return
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Nova carta", callback_data="studio_go|addcarta"), InlineKeyboardButton("✏️ Editar cartas", callback_data="studio_go|editcarta")],
        [InlineKeyboardButton("📂 Categorias & grupos", callback_data="studio_go|categorias"), InlineKeyboardButton("📝 Textos públicos", callback_data="studio_go|textos")],
        [InlineKeyboardButton("📚 Catálogo", callback_data="studio_go|catalogo"), InlineKeyboardButton("📦 Mover artista", callback_data="studio_go|moverartista")],
    ])
    teclado.inline_keyboard.append([InlineKeyboardButton("🎉 Eventos", callback_data="evento|admin")])
    await query.answer()
    await query.edit_message_text("🛠️ <b>Ecco-Mix Studio</b>\n\nEscolha o que deseja fazer:", reply_markup=teclado, parse_mode="HTML")

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

    total_atualizadas = await _regenerar_cartas_moldura_tier(context, tier, png_bytes, update.effective_chat.id)
    await update.message.reply_text(f"Moldura salva e aplicada em {total_atualizadas} carta(s) existente(s) do {TIERS_CONFIG[tier]['nome']}.")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT file_id FROM cartas WHERE tier=%s AND event_id IS NULL AND media_type='photo' ORDER BY id LIMIT 1", (tier,))
    amostra = cur.fetchone()
    cur.close()
    conn.close()
    if amostra:
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=amostra[0], caption=f"Prévia — {TIERS_CONFIG[tier]['nome']}")

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

    atualizadas = await _regenerar_cartas_moldura_tier(context, tier, None, update.effective_chat.id)
    await update.message.reply_text(f"Moldura removida; {atualizadas} carta(s) restauradas da imagem original.")

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
    for t in range(1, 7):
        status = "✅ Moldura PNG ativa" if t in ativas else f"➖ Sem moldura (apenas {LARGURA_CARTA}×{ALTURA_CARTA})"
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
    cur.execute("SELECT id, subcategoria_id, nome FROM subsubcategorias ORDER BY id ASC")
    subsubs = cur.fetchall()
    cur.close()
    conn.close()

    mapa_subsubs = {}
    for ssid, sid, ssnome in subsubs:
        mapa_subsubs.setdefault(sid, []).append(f"<code>[{ssid}]</code> {esc(ssnome)}")

    mapa_subs = {}
    for sid, cid, snome in subs:
        item = f"<code>[{sid}]</code> {esc(snome)}"
        if sid in mapa_subsubs:
            item += f" ({', '.join(mapa_subsubs[sid])})"
        mapa_subs.setdefault(cid, []).append(item)

    linhas = ["📂 <b>Categorias, Subcategorias & Grupos</b>\n"]
    for cid, cnome in cats:
        linhas.append(f"• <code>[{cid}]</code> <b>{esc(cnome)}</b>")
        if cid in mapa_subs:
            linhas.append(f"   ↳ {', '.join(mapa_subs[cid])}")

    await update.message.reply_text("\n".join(linhas)[:4000], parse_mode="HTML")

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

async def cmd_deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        await update.effective_message.reply_text("Somente o administrador principal pode zerar o histórico de cartas de todos.")
        return
    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑️ Apagar coleções de todos", callback_data="deleteall|confirm"),
            InlineKeyboardButton("Cancelar", callback_data="deleteall|cancel"),
        ]
    ])
    await update.effective_message.reply_text(
        "⚠️ Isso vai zerar as cartas obtidas por todos e cancelar as trocas pendentes. As cartas cadastradas e os giros permanecem. Confirmar?",
        reply_markup=teclado,
    )

async def callback_deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Somente o administrador principal pode zerar o histórico de cartas de todos.", show_alert=True)
        return
    if query.data == "deleteall|cancel":
        await query.answer("Cancelado.")
        await query.edit_message_text("Operação cancelada; as coleções continuam intactas.")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM inventario")
    apagados = cur.rowcount
    cur.execute("DELETE FROM trocas WHERE status = 'pendente'")
    trocas_canceladas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    await query.answer("Coleções zeradas.")
    await query.edit_message_text(
        f"✅ Coleções zeradas: {apagados} registros de inventário removidos e {trocas_canceladas} trocas pendentes canceladas. As cartas cadastradas continuam no catálogo."
    )

async def cmd_moverartista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or await bloqueio_nao_admin(update):
        return
    nome = " ".join(context.args).strip()
    if not nome:
        await msg.reply_text("Informe o nome do artista/personagem. Você também pode iniciar pelo botão no Studio.", parse_mode="HTML")
        return

    await iniciar_movimento_artista(msg, update.effective_user.id, context, nome)

async def iniciar_movimento_artista(msg, user_id: int, context: ContextTypes.DEFAULT_TYPE, nome: str):

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome, c.tier, cat.nome, sub.nome, subsub.nome
        FROM cartas c
        JOIN categorias cat ON cat.id = c.categoria_id
        LEFT JOIN subcategorias sub ON sub.id = c.subcategoria_id
        LEFT JOIN subsubcategorias subsub ON subsub.id = c.subsubcategoria_id
        WHERE LOWER(TRIM(c.nome)) = LOWER(TRIM(%s))
        ORDER BY c.tier, c.id
    """, (nome,))
    cartas = cur.fetchall()
    cur.close()
    conn.close()
    if not cartas:
        await msg.reply_text(f"Não encontrei cartas cadastradas com o nome <b>{esc(nome)}</b>.", parse_mode="HTML")
        return

    context.user_data["mv_artist_admin"] = user_id
    context.user_data["mv_artist_chat"] = msg.chat_id
    context.user_data["mv_artist_name"] = cartas[0][1]
    context.user_data["mv_artist_ids"] = [row[0] for row in cartas]
    context.user_data["mv_artist_tiers"] = sorted({row[2] for row in cartas})
    caminhos = sorted({" / ".join(x for x in row[3:] if x) for row in cartas})
    resumo = (
        f"📦 <b>{esc(cartas[0][1])}</b>: {len(cartas)} carta(s), "
        f"tiers {', '.join(str(t) for t in context.user_data['mv_artist_tiers'])}\n"
        f"Local atual: {esc('; '.join(caminhos))}\n\nEscolha a categoria de destino:"
    )
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, nome FROM categorias ORDER BY nome")
    categorias = cur.fetchall()
    cur.close()
    conn.close()
    botoes = []
    linha = []
    for cat_id, cat_nome in categorias:
        linha.append(InlineKeyboardButton(cat_nome[:50], callback_data=f"mvartist|cat|{cat_id}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    botoes.append([InlineKeyboardButton("Cancelar", callback_data="mvartist|cancel")])
    await msg.reply_text(resumo, reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")

async def studio_moverartista_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not eh_admin(query.from_user.id):
        await query.answer("Apenas administradores podem mover cartas.", show_alert=True)
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["mv_artist_admin"] = query.from_user.id
    context.user_data["mv_artist_chat"] = query.message.chat_id
    texto, markup = _montar_lista_artistas_mover(0)
    await query.answer()
    await query.message.reply_text(texto, reply_markup=markup, parse_mode="HTML")
    return MV_ARTIST_NAME

def _montar_lista_artistas_mover(page: int, busca: str = ""):
    page_size = 10
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT LOWER(TRIM(nome))) FROM cartas WHERE nome ILIKE %s",(f"%{busca}%",))
    total = cur.fetchone()[0]
    total_paginas = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_paginas - 1))
    cur.execute("""
        SELECT MIN(id), MIN(nome), COUNT(*)
        FROM cartas WHERE nome ILIKE %s
        GROUP BY LOWER(TRIM(nome))
        ORDER BY LOWER(MIN(nome))
        LIMIT %s OFFSET %s
    """, (f"%{busca}%", page_size, page * page_size))
    artistas = cur.fetchall()
    cur.close()
    conn.close()

    botoes = []
    linha = []
    for carta_id, nome, qtd in artistas:
        linha.append(InlineKeyboardButton(f"{nome[:42]} ({qtd})", callback_data=f"mvlist|pick|{carta_id}"))
        if len(linha) == 2:
            botoes.append(linha)
            linha = []
    if linha:
        botoes.append(linha)
    botoes.append([InlineKeyboardButton("🔍 Pesquisar artista/personagem", callback_data="mvlist|search")])
    if total_paginas > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"mvlist|page|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_paginas}", callback_data="mvlist|noop"))
        if page + 1 < total_paginas:
            nav.append(InlineKeyboardButton("Próxima ➡️", callback_data=f"mvlist|page|{page + 1}"))
        botoes.append(nav)
    botoes.append([InlineKeyboardButton("Cancelar", callback_data="mvlist|cancel")])
    texto = "📦 <b>Mover artista/personagem</b>\n\nEscolha nas cartas quem deseja mover:"
    return texto, InlineKeyboardMarkup(botoes)

async def studio_moverartista_lista_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not eh_admin(query.from_user.id) or context.user_data.get("mv_artist_admin") != query.from_user.id:
        await query.answer("Esse menu pertence a outro admin.", show_alert=True)
        return ConversationHandler.END
    partes = query.data.split("|")
    acao = partes[1]
    if acao == "cancel":
        await query.answer("Cancelado.")
        await query.edit_message_text("Movimentação cancelada.")
        context.user_data.clear()
        return ConversationHandler.END
    if acao == "noop":
        await query.answer()
        return MV_ARTIST_NAME
    if acao == "search":
        context.user_data["mv_artist_search_pending"] = True
        await query.answer(); await query.message.reply_text("Digite o nome do artista/personagem para pesquisar:")
        return MV_ARTIST_NAME
    if acao == "page":
        texto, markup = _montar_lista_artistas_mover(int(partes[2]),context.user_data.get("mv_artist_search",""))
        await query.answer()
        await query.edit_message_text(texto, reply_markup=markup, parse_mode="HTML")
        return MV_ARTIST_NAME
    if acao == "pick":
        carta_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM cartas WHERE id = %s", (carta_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            await query.answer("Essa carta não existe mais.", show_alert=True)
            return MV_ARTIST_NAME
        await query.answer("Artista selecionado.")
        await query.edit_message_text(f"Artista selecionado: <b>{esc(row[0])}</b>", parse_mode="HTML")
        await iniciar_movimento_artista(query.message, query.from_user.id, context, row[0])
        return ConversationHandler.END
    await query.answer()
    return MV_ARTIST_NAME

async def studio_moverartista_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("mv_artist_search_pending"):
        return MV_ARTIST_NAME
    context.user_data["mv_artist_search_pending"] = False
    busca=(update.effective_message.text or "").strip()
    context.user_data["mv_artist_search"]=busca
    texto,markup=_montar_lista_artistas_mover(0,busca)
    await update.effective_message.reply_text(texto,reply_markup=markup,parse_mode="HTML")
    return MV_ARTIST_NAME

async def studio_moverartista_receber_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not eh_admin(update.effective_user.id):
        return ConversationHandler.END
    nome = (update.effective_message.text or "").strip()
    if not nome:
        await update.effective_message.reply_text("Digite um nome válido ou use /cancelar.")
        return MV_ARTIST_NAME
    await iniciar_movimento_artista(update.effective_message, update.effective_user.id, context, nome)
    return ConversationHandler.END

async def studio_moverartista_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Movimentação cancelada.")
    return ConversationHandler.END

async def callback_moverartista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not eh_admin(query.from_user.id):
        await query.answer("Apenas administradores podem mover cartas.", show_alert=True)
        return
    if (
        context.user_data.get("mv_artist_admin") != query.from_user.id
        or context.user_data.get("mv_artist_chat") != query.message.chat_id
    ):
        await query.answer("Esse menu expirou. Use /moverartista novamente.", show_alert=True)
        return

    partes = query.data.split("|")
    acao = partes[1]
    if acao == "cancel":
        context.user_data.pop("mv_artist_name", None)
        context.user_data.pop("mv_artist_ids", None)
        context.user_data.pop("mv_artist_tiers", None)
        context.user_data.pop("mv_artist_admin", None)
        context.user_data.pop("mv_artist_chat", None)
        await query.answer("Movimentação cancelada.")
        await query.edit_message_text("Movimentação cancelada.")
        return

    if acao == "cat":
        cat_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM categorias WHERE id = %s", (cat_id,))
        categoria = cur.fetchone()
        cur.execute("SELECT id, nome FROM subcategorias WHERE categoria_id = %s ORDER BY nome", (cat_id,))
        subs = cur.fetchall()
        cur.close()
        conn.close()
        if not categoria:
            await query.answer("Categoria não encontrada.", show_alert=True)
            return
        context.user_data["mv_artist_cat"] = cat_id
        context.user_data["mv_artist_cat_name"] = categoria[0]
        botoes = []
        linha = []
        for sub_id, sub_nome in subs:
            linha.append(InlineKeyboardButton(sub_nome[:50], callback_data=f"mvartist|sub|{sub_id}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
        botoes.append([InlineKeyboardButton("Mover direto para a categoria", callback_data="mvartist|no_sub")])
        botoes.append([InlineKeyboardButton("Cancelar", callback_data="mvartist|cancel")])
        await query.answer()
        await query.edit_message_text(
            f"Destino: <b>{esc(categoria[0])}</b>. Escolha uma subcategoria ou mova direto para a categoria:",
            reply_markup=InlineKeyboardMarkup(botoes),
            parse_mode="HTML",
        )
        return

    if acao == "sub":
        sub_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM subcategorias WHERE id = %s AND categoria_id = %s", (sub_id, context.user_data.get("mv_artist_cat")))
        sub = cur.fetchone()
        cur.execute("SELECT id, nome FROM subsubcategorias WHERE subcategoria_id = %s ORDER BY nome", (sub_id,))
        grupos = cur.fetchall()
        cur.close()
        conn.close()
        if not sub:
            await query.answer("Subcategoria inválida para esta categoria.", show_alert=True)
            return
        context.user_data["mv_artist_sub"] = sub_id
        if not grupos:
            await query.answer("Destino selecionado.")
            await _salvar_movimento_artista(query, context, sub_id=sub_id)
            return
        botoes = []
        linha = []
        for grupo_id, grupo_nome in grupos:
            linha.append(InlineKeyboardButton(grupo_nome[:50], callback_data=f"mvartist|group|{grupo_id}"))
            if len(linha) == 2:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)
        botoes.append([InlineKeyboardButton("Mover direto para a subcategoria", callback_data="mvartist|no_group")])
        botoes.append([InlineKeyboardButton("Cancelar", callback_data="mvartist|cancel")])
        await query.answer()
        await query.edit_message_text(
            f"Subcategoria: <b>{esc(sub[0])}</b>. Escolha o grupo ou mova direto para a subcategoria:",
            reply_markup=InlineKeyboardMarkup(botoes),
            parse_mode="HTML",
        )
        return

    if acao == "group":
        group_id = int(partes[2])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT nome FROM subsubcategorias WHERE id = %s AND subcategoria_id = %s", (group_id, context.user_data.get("mv_artist_sub")))
        grupo = cur.fetchone()
        cur.close()
        conn.close()
        if not grupo:
            await query.answer("Grupo inválido para esta subcategoria.", show_alert=True)
            return
        await query.answer("Destino selecionado.")
        await _salvar_movimento_artista(query, context, sub_id=context.user_data["mv_artist_sub"], subsub_id=group_id)
        return

    if acao == "no_sub":
        await query.answer("Destino selecionado.")
        await _salvar_movimento_artista(query, context)
        return

    if acao == "no_group":
        await query.answer("Destino selecionado.")
        await _salvar_movimento_artista(query, context, sub_id=context.user_data["mv_artist_sub"])

async def _salvar_movimento_artista(query, context, sub_id=None, subsub_id=None):
    cat_id = context.user_data["mv_artist_cat"]
    carta_ids = context.user_data["mv_artist_ids"]
    nome = context.user_data["mv_artist_name"]
    tiers = context.user_data["mv_artist_tiers"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT usa_tiers FROM categorias WHERE id = %s", (cat_id,))
    usa_tiers = bool(cur.fetchone()[0])
    if not usa_tiers and len(tiers) > 1:
        cur.close()
        conn.close()
        await query.edit_message_text(
            "Não movi as cartas: o destino está configurado com tier único e este artista tem vários tiers. "
            "Ative os cinco tiers na categoria de destino ou escolha outra categoria."
        )
        return
    tiers_destino = tiers if usa_tiers else [1]
    cur.execute(
        "SELECT id FROM cartas WHERE categoria_id = %s AND LOWER(TRIM(nome)) = LOWER(TRIM(%s)) "
        "AND tier = ANY(%s) AND NOT (id = ANY(%s)) LIMIT 1",
        (cat_id, nome, tiers_destino, carta_ids),
    )
    conflito = cur.fetchone()
    if conflito:
        cur.close()
        conn.close()
        await query.edit_message_text(
            f"Não movi as cartas: já existe outra versão de <b>{esc(nome)}</b> na categoria de destino. "
            "Remova ou reorganize essa entrada primeiro.",
            parse_mode="HTML",
        )
        return
    if usa_tiers:
        cur.execute(
            "UPDATE cartas SET categoria_id = %s, subcategoria_id = %s, subsubcategoria_id = %s WHERE id = ANY(%s)",
            (cat_id, sub_id, subsub_id, carta_ids),
        )
    else:
        cur.execute(
            "UPDATE cartas SET categoria_id = %s, subcategoria_id = %s, subsubcategoria_id = %s, tier = 1, peso_drop = %s WHERE id = ANY(%s)",
            (cat_id, sub_id, subsub_id, TIERS_CONFIG[1]["peso"], carta_ids),
        )
    movidas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    await query.edit_message_text(
        f"✅ Movi <b>{movidas}</b> carta(s) de <b>{esc(nome)}</b> para "
        f"<b>{esc(context.user_data['mv_artist_cat_name'])}</b>.",
        parse_mode="HTML",
    )
    for key in ("mv_artist_name", "mv_artist_ids", "mv_artist_tiers", "mv_artist_admin", "mv_artist_chat", "mv_artist_cat", "mv_artist_sub", "mv_artist_cat_name"):
        context.user_data.pop(key, None)

async def cmd_dar_giros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or await bloqueio_nao_admin(update):
        return

    reply = msg.reply_to_message
    # CommandHandler.args can be empty for some group/topic updates; recover the text args.
    args = context.args or (msg.text or msg.caption or "").split()[1:]
    # Ignora apenas o reply automático de cabeçalho de tópico em grupos com Fóruns
    eh_servico_topico = reply is not None and any(
        getattr(reply, attr, None) is not None
        for attr in (
            "forum_topic_created",
            "forum_topic_edited",
            "forum_topic_closed",
            "forum_topic_reopened",
            "general_forum_topic_hidden",
            "general_forum_topic_unhidden",
        )
    )
    eh_reply_real = reply is not None and not eh_servico_topico

    alvo_id = None
    alvo_username = ""

    try:
        if eh_reply_real and len(args) == 1:
            alvo = reply.from_user
            if not alvo or alvo.is_bot:
                await msg.reply_text("⚠️ Responda à mensagem de um jogador válido (não de um bot).")
                return
            alvo_id = alvo.id
            alvo_username = alvo.username or ""
            qtd = int(args[0])
            verificar_usuario(alvo_id, alvo_username)

        elif len(args) >= 2:
            qtd = int(args[-1])
            alvo_str = " ".join(args[:-1]).strip()

            if alvo_str.isdigit():
                alvo_id = int(alvo_str)
                verificar_usuario(alvo_id)
            else:
                busca = alvo_str.lstrip("@").strip()
                conn = get_db()
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT user_id, username FROM usuarios
                    WHERE LOWER(username) = LOWER(%s)
                       OR LOWER(TRIM(apelido)) = LOWER(%s)
                    LIMIT 1
                    """,
                    (busca, busca),
                )
                row = cur.fetchone()
                cur.close()
                conn.close()

                if not row:
                    await msg.reply_text(
                        f"⚠️ Jogador <b>{esc(alvo_str)}</b> não encontrado.\n"
                        f"Peça para ele usar <code>/start</code> ou <code>/girar</code> primeiro, ou informe o <b>ID numérico</b>.",
                        parse_mode="HTML",
                    )
                    return

                alvo_id, uname_db = row
                alvo_username = uname_db or ""
        else:
            raise ValueError
    except (IndexError, ValueError):
        await msg.reply_text(
            "🍃 <b>Como usar /dargiros:</b>\n"
            "• Responda à mensagem da pessoa: <code>/dargiros 5</code>\n"
            "• Por ID: <code>/dargiros 123456789 5</code>\n"
            "• Por @ ou apelido: <code>/dargiros @usuario 5</code>",
            parse_mode="HTML",
        )
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE usuarios
        SET giros_restantes = GREATEST(0, giros_restantes + %s)
        WHERE user_id = %s
        RETURNING giros_restantes
        """,
        (qtd, alvo_id),
    )
    res = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    novo_total = res[0] if res else qtd
    apelido_exib, _ = obter_apelido(alvo_id, alvo_username)
    sinal = "+" if qtd >= 0 else ""

    await msg.reply_text(
        f"🍃 <b>{sinal}{qtd}</b> giros para 🫧 <b>{esc(apelido_exib)}</b> (<code>{alvo_id}</code>)\n"
        f"🎯 Total disponível agora: <b>{novo_total}</b>",
        parse_mode="HTML",
    )

def _teclado_dargiros(target_id: int, user_id: int):
    values=(10,50,100,500,1000)
    buttons=[]; row=[]
    for value in values:
        row.append(InlineKeyboardButton(f"+{value}",callback_data=f"darg|q|{target_id}|{value}|{user_id}"))
        if len(row)==2: buttons.append(row); row=[]
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("✏️ Digitar outra quantidade",callback_data=f"darg|custom|{target_id}|0|{user_id}")])
    return InlineKeyboardMarkup(buttons)

async def _dar_giros_alvo(bot, chat_id: int, target_id: int, quantidade: int):
    conn=get_db(); cur=conn.cursor()
    cur.execute("UPDATE usuarios SET giros_restantes=COALESCE(giros_restantes,0)+%s WHERE user_id=%s RETURNING giros_restantes",(quantidade,target_id))
    row=cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return row[0] if row else None

async def callback_dargiros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not eh_admin(q.from_user.id): await q.answer("Apenas administradores.",show_alert=True); return
    _,action,target_s,amount_s,owner_s=q.data.split("|")
    target_id=int(target_s)
    if int(owner_s)!=q.from_user.id: await q.answer("Esse menu não é seu.",show_alert=True); return
    if action=="custom":
        context.user_data["dargiros_pendente"]=target_id
        await q.answer(); await q.message.reply_text("Digite quantos giros deseja adicionar (número inteiro positivo):"); return
    amount=int(amount_s)
    total=await _dar_giros_alvo(context.bot,q.message.chat_id,target_id,amount)
    await q.answer("Giros adicionados.")
    await q.edit_message_text(f"✅ +{amount} giros para <code>{target_id}</code>. Total disponível: <b>{total}</b>.",parse_mode="HTML")

async def cmd_dar_giros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message; user=update.effective_user
    if not msg or not user or not eh_admin(user.id): return
    args=context.args or (msg.text or "").split()[1:]
    reply=msg.reply_to_message.from_user if msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.from_user.is_bot else None
    target_id=None; target_username=""; quantidade=None
    if reply:
        target_id=reply.id; target_username=reply.username or ""; verificar_usuario(target_id,target_username)
        if args:
            try: quantidade=int(args[0])
            except ValueError: await msg.reply_text("A quantidade precisa ser um número inteiro."); return
    elif len(args)>=1:
        has_amount=len(args)>=2 and args[-1].lstrip("+").isdigit()
        target_text=" ".join(args[:-1] if has_amount else args).strip()
        if has_amount: quantidade=int(args[-1])
        if target_text.isdigit():
            target_id=int(target_text); verificar_usuario(target_id)
        else:
            target=_resolver_jogador(target_text)
            if not target: await msg.reply_text("Jogador não encontrado; informe @, apelido ou ID."); return
            target_id,target_username,_=target
    else:
        await msg.reply_text("Responda à mensagem da pessoa com /dargiros ou use /dargiros ID."); return
    if quantidade is not None:
        if quantidade<=0: await msg.reply_text("Digite uma quantidade positiva."); return
        total=await _dar_giros_alvo(context.bot,msg.chat_id,target_id,quantidade)
        await msg.reply_text(f"✅ +{quantidade} giros para <code>{target_id}</code>. Total disponível: <b>{total}</b>.",parse_mode="HTML")
        return
    conn=get_db(); cur=conn.cursor(); cur.execute("SELECT giros_restantes FROM usuarios WHERE user_id=%s",(target_id,)); row=cur.fetchone(); cur.close(); conn.close()
    await msg.reply_text(f"Quantos giros deseja adicionar para <code>{target_id}</code>? Saldo atual: <b>{row[0] if row else 0}</b>.",reply_markup=_teclado_dargiros(target_id,user.id),parse_mode="HTML")

async def cmd_add_adm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in SUPER_ADMINS:
        await update.effective_message.reply_text("Somente o administrador principal pode alterar as funções da equipe.")
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
    cur.execute("INSERT INTO admins (user_id,funcao) VALUES (%s,'admin') ON CONFLICT(user_id) DO UPDATE SET funcao='admin'", (novo_adm,))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"🛡️ Usuário <code>{novo_adm}</code> agora é administrador.", parse_mode="HTML")

async def cmd_add_editor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in SUPER_ADMINS:
        await update.effective_message.reply_text("Somente o administrador principal pode alterar as funções da equipe.")
        return
    try:
        target_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Uso: <code>/addeditor ID</code>", parse_mode="HTML"); return
    conn=get_db(); cur=conn.cursor()
    cur.execute("INSERT INTO admins(user_id,funcao) VALUES(%s,'editor') ON CONFLICT(user_id) DO UPDATE SET funcao='editor'", (target_id,))
    conn.commit(); cur.close(); conn.close()
    await update.effective_message.reply_text(f"Editor <code>{target_id}</code> adicionado. Pode editar textos públicos e do painel.", parse_mode="HTML")

async def cmd_ver_adms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not eh_admin(update.effective_user.id):
        await update.effective_message.reply_text("Apenas administradores podem consultar a equipe."); return
    await update.effective_message.reply_text("👥 <b>Administradores e editores</b> — use as setas para navegar.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Abrir lista", callback_data="studio_ui|admins|0")]]), parse_mode="HTML")

async def cmd_rem_adm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in SUPER_ADMINS:
        await update.effective_message.reply_text("Somente o administrador principal pode alterar as funções da equipe.")
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
    print("🚀 [1/4] Iniciando servidor HTTP...", flush=True)
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    app.add_handler(MessageHandler(filters.ALL, handler_usuario_banido), group=-1)
    app.add_handler(CallbackQueryHandler(handler_usuario_banido, pattern=r"."), group=-1)

    from eventos import GerenciadorEventos
    eventos_manager = GerenciadorEventos({
        "get_db": get_db, "esc": esc, "eh_admin": eh_admin,
        "resolver_jogador": _resolver_jogador, "verificar_usuario": verificar_usuario,
        "executar_sorteio": executar_sorteio,
        "tier_nome": lambda tier: TIERS_CONFIG[tier]["nome"],
        "tags_por_carta": _tags_por_carta,
        "retirar_inventario": _retirar_inventario, "adicionar_inventario": _adicionar_inventario,
        "regenerar_moldura_evento": _regenerar_cartas_moldura_evento,
    })
    eventos_manager.registrar(app)

    async def evento_midia_handler(update, context):
        if await handler_studio_moldura_media(update, context):
            return
        if context.user_data.get("evento_pendente"):
            manager = context.application.bot_data.get("eventos_manager")
            if manager:
                await manager.processar_midia(update, context)

    # Assistente interativo /addcarta (com 3 níveis)
    conv_carta = ConversationHandler(
        entry_points=[
            MessageHandler(filters.COMMAND & filters.Regex(r"^/addcarta[-_].+"), wiz_start_event_command),
            CommandHandler("addcarta", wiz_start),
            CallbackQueryHandler(wiz_start, pattern=r"^studio_go\|addcarta$"),
            CallbackQueryHandler(wiz_start_event, pattern=r"^studio_go\|addevent\|\d+$"),
            CallbackQueryHandler(wiz_start_add_tier, pattern=r"^studio_go\|addtier\|"),
        ],
        states={
            ST_CAT: [CallbackQueryHandler(wiz_escolher_cat, pattern=r"^wiz_(cat\|.+|new_cat)$")],
            ST_NEW_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_receber_nova_cat)],
            ST_ASK_SUB_CHOICE: [CallbackQueryHandler(wiz_decidir_sub, pattern=r"^wiz_sub_(sim|nao)$")],
            ST_SUB: [CallbackQueryHandler(wiz_escolher_sub, pattern=r"^wiz_(sub\|.+|new_sub)$")],
            ST_NEW_SUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_receber_nova_sub)],
            ST_ASK_SUBSUB_CHOICE: [CallbackQueryHandler(wiz_decidir_subsub, pattern=r"^wiz_subsub_(sim|nao)$")],
            ST_SUBSUB: [CallbackQueryHandler(wiz_escolher_subsub, pattern=r"^wiz_(subsub\|.+|new_subsub|no_subsub)$")],
            ST_NEW_SUBSUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_receber_nova_subsub)],
            ST_NOME: [CallbackQueryHandler(wiz_escolher_nome, pattern=r"^wiz_(nome\|.+|new_nome|new_subsub_from_nome)$")],
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

    # Assistente interativo /editcarta (com paginação & 3 níveis)
    conv_edit_carta = ConversationHandler(
        entry_points=[
            CommandHandler("editcarta", ed_start),
            CallbackQueryHandler(ed_start, pattern=r"^studio_go\|editcarta$"),
        ],
        states={
            ED_MENU: [
                CallbackQueryHandler(ed_menu_callback, pattern=r"^(ed_(pick\||page\||searchpage\||search$|tags$|delete(_confirm|_cancel)?$|back_list$|noop$|op\||salvar$)|studio_menu$)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ed_receber_id_texto),
            ],
            ED_NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ed_receber_nome)],
            ED_CAT: [CallbackQueryHandler(ed_escolher_cat, pattern=r"^ed_(cat\||new_cat$|voltar$)")],
            ED_NEW_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ed_receber_nova_cat)],
            ED_ASK_SUB: [CallbackQueryHandler(ed_decidir_sub, pattern=r"^ed_sub_(sim|nao)$")],
            ED_SUB: [CallbackQueryHandler(ed_escolher_sub, pattern=r"^ed_(sub\||new_sub$|sem_sub$)")],
            ED_NEW_SUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, ed_receber_nova_sub)],
            ED_ASK_SUBSUB: [CallbackQueryHandler(ed_decidir_subsub, pattern=r"^ed_subsub_(sim|nao)$")],
            ED_SUBSUB: [CallbackQueryHandler(ed_escolher_subsub, pattern=r"^ed_(subsub\||new_subsub$|sem_subsub$)")],
            ED_NEW_SUBSUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, ed_receber_nova_subsub)],
            ED_MIDIA: [MessageHandler(filters.PHOTO | filters.Document.IMAGE | filters.ANIMATION, ed_receber_midia)],
            ED_TIER: [CallbackQueryHandler(ed_escolher_tier, pattern=r"^ed_(tier\||tlock\||voltar$)")],
        },
        fallbacks=[
            CommandHandler("cancelar", ed_cancel),
            CallbackQueryHandler(ed_cancel, pattern=r"^ed_cancel$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv_edit_carta)

    # Gerenciador interativo /categorias (com 3 níveis)
    conv_catmgr = ConversationHandler(
        entry_points=[
            CommandHandler("categorias", catmgr_start),
            CallbackQueryHandler(catmgr_start, pattern=r"^studio_go\|categorias$"),
        ],
        states={
            CM_HOME: [CallbackQueryHandler(catmgr_callback, pattern=r"^catmgr\|(cat\||new_cat$|search\||home$|close$|studio_menu$)")],
            CM_CAT_VIEW: [CallbackQueryHandler(catmgr_callback, pattern=r"^catmgr\|(ren_cat$|toggle_tiers$|tiers_confirm$|tiers_cancel$|new_sub$|del_cat$|search\||sub\||home$|close$|studio_menu$)")],
            CM_NEW_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, catmgr_receber_nova_cat)],
            CM_REN_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, catmgr_receber_ren_cat)],
            CM_SUB_VIEW: [CallbackQueryHandler(catmgr_callback, pattern=r"^catmgr\|(ren_sub$|new_subsub$|del_sub$|search\||subsub\||cat\||home$|close$|studio_menu$)")],
            CM_NEW_SUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, catmgr_receber_nova_sub)],
            CM_REN_SUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, catmgr_receber_ren_sub)],
            CM_SUBSUB_VIEW: [CallbackQueryHandler(catmgr_callback, pattern=r"^catmgr\|(ren_subsub$|del_subsub$|sub\||home$|close$)")],
            CM_NEW_SUBSUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, catmgr_receber_nova_subsub)],
            CM_REN_SUBSUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, catmgr_receber_ren_subsub)],
        },
        fallbacks=[
            CommandHandler("cancelar", catmgr_cancel),
            CallbackQueryHandler(catmgr_cancel, pattern=r"^catmgr\|close$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv_catmgr)

    # Gerenciador de textos públicos, GIF do /start e emojis Premium (/textos)
    conv_txmgr = ConversationHandler(
        entry_points=[
            CommandHandler("textos", txmgr_start),
            CallbackQueryHandler(txmgr_start, pattern=r"^studio_go\|textos$"),
        ],
        states={
            TX_HOME: [CallbackQueryHandler(txmgr_callback, pattern=r"^txmgr\|(item\||page\||noop$|start_media$|tiers$|close$)")],
            TX_VIEW: [CallbackQueryHandler(txmgr_callback, pattern=r"^txmgr\|(edit$|reset$|home$)")],
            TX_EDIT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, txmgr_receber_texto)],
            TX_START_MEDIA: [
                MessageHandler(filters.ANIMATION | filters.PHOTO, txmgr_receber_start_media),
                CallbackQueryHandler(txmgr_callback, pattern=r"^txmgr\|(del_start_media$|home$)"),
            ],
            TX_TIERS_MENU: [CallbackQueryHandler(txmgr_callback, pattern=r"^txmgr\|(tier\||home$)")],
            TX_TIER_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, txmgr_receber_tier_emoji)],
        },
        fallbacks=[
            CommandHandler("cancelar", txmgr_cancel),
            CallbackQueryHandler(txmgr_cancel, pattern=r"^txmgr\|close$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv_txmgr)

    conv_mover_artista = ConversationHandler(
        entry_points=[CallbackQueryHandler(studio_moverartista_start, pattern=r"^studio_go\|moverartista$")],
        states={
            MV_ARTIST_NAME: [CallbackQueryHandler(studio_moverartista_lista_callback, pattern=r"^mvlist\|"), MessageHandler(filters.TEXT & ~filters.COMMAND, studio_moverartista_buscar)],
        },
        fallbacks=[CommandHandler("cancelar", studio_moverartista_cancelar)],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv_mover_artista)

    # Comandos de Jogadores
    app.add_handler(CommandHandler(["start", "ajuda"], cmd_start))
    app.add_handler(CommandHandler(["comandos", "comados"], cmd_comandos))
    app.add_handler(CommandHandler("apelido", cmd_apelido))
    app.add_handler(CommandHandler("perfil", cmd_perfil))
    app.add_handler(CallbackQueryHandler(callback_perfil, pattern=r"^perfil\|"))
    app.add_handler(CommandHandler("casar", cmd_casar))
    app.add_handler(CommandHandler("divorciar", cmd_divorciar))
    app.add_handler(CallbackQueryHandler(callback_casamento, pattern=r"^casar\|"))
    app.add_handler(CommandHandler("meuid", cmd_meu_id))
    app.add_handler(CommandHandler("girar", cmd_girar))
    app.add_handler(CommandHandler(["catalogo", "cartas"], cmd_catalogo))
    app.add_handler(CommandHandler("artista", cmd_artista))
    app.add_handler(CommandHandler(["ranking", "top"], cmd_ranking))
    app.add_handler(CommandHandler(["colecao", "col"], cmd_colecao))
    app.add_handler(CallbackQueryHandler(callback_colecao, pattern=r"^colecao\|"))
    app.add_handler(CommandHandler("tag", cmd_tag))
    app.add_handler(CommandHandler("tags", cmd_tags_admin))
    app.add_handler(CallbackQueryHandler(callback_tagadm, pattern=r"^tagadm\|"))
    app.add_handler(CallbackQueryHandler(callback_tag, pattern=r"^tag\|"))
    app.add_handler(CallbackQueryHandler(callback_col_tag, pattern=r"^col\|"))
    app.add_handler(CommandHandler("carta", cmd_carta))
    app.add_handler(CallbackQueryHandler(callback_carta_busca, pattern=r"^carta\|"))
    app.add_handler(CommandHandler("trocar", cmd_trocar))
    app.add_handler(CommandHandler("doar", cmd_doar))
    app.add_handler(CommandHandler("wishlist", cmd_wishlist))
    app.add_handler(CommandHandler("loja", cmd_loja))

    # Comandos de Admin (Studio)
    app.add_handler(CommandHandler("studio", cmd_studio_help))
    app.add_handler(CallbackQueryHandler(studio_menu_callback, pattern=r"^studio_menu$"))
    app.add_handler(CallbackQueryHandler(studio_ui_callback, pattern=r"^studio_ui\|"))
    app.add_handler(CommandHandler("moldura", cmd_set_moldura))
    app.add_handler(CommandHandler("remmoldura", cmd_rem_moldura))
    app.add_handler(CommandHandler("molduras", cmd_status_molduras))
    app.add_handler(CommandHandler("listacat", cmd_lista_cat))
    app.add_handler(CommandHandler("editcat", cmd_edit_cat))
    app.add_handler(CommandHandler("editsub", cmd_edit_sub))
    app.add_handler(CommandHandler("delcarta", cmd_del_carta))
    app.add_handler(CommandHandler("deleteall", cmd_deleteall))
    app.add_handler(CallbackQueryHandler(callback_deleteall, pattern=r"^deleteall\|"))
    app.add_handler(CallbackQueryHandler(callback_moverartista, pattern=r"^mvartist\|"))
    app.add_handler(CommandHandler("dargiros", cmd_dar_giros))
    app.add_handler(CommandHandler("addadm", cmd_add_adm))
    app.add_handler(CommandHandler("remadm", cmd_rem_adm))
    app.add_handler(CommandHandler("addeditor", cmd_add_editor))
    app.add_handler(CommandHandler("veradms", cmd_ver_adms))

    # Botões Interativos do Jogo
    app.add_handler(CallbackQueryHandler(callback_girar_categoria, pattern=r"^rcat\|"))
    app.add_handler(CallbackQueryHandler(callback_girar_subcategoria, pattern=r"^rsub\|"))
    app.add_handler(CallbackQueryHandler(callback_girar_subsubcategoria, pattern=r"^rsubsub\|"))
    app.add_handler(CallbackQueryHandler(callback_girar_mais_opcoes, pattern=r"^rmore\|"))
    app.add_handler(CallbackQueryHandler(callback_girar_quantidade, pattern=r"^rqtd\|"))
    app.add_handler(CallbackQueryHandler(callback_dargiros, pattern=r"^darg\|"))
    app.add_handler(CallbackQueryHandler(callback_ranking_cid, pattern=r"^rank_cid\|"))
    app.add_handler(CallbackQueryHandler(callback_catalogo, pattern=r"^catlist\|"))
    app.add_handler(CallbackQueryHandler(cmd_catalogo, pattern=r"^studio_go\|catalogo$"))
    app.add_handler(CallbackQueryHandler(callback_troca, pattern=r"^trade\|"))
    app.add_handler(CallbackQueryHandler(callback_troca_ui, pattern=r"^tradeui\|"))
    app.add_handler(CallbackQueryHandler(callback_doarui, pattern=r"^doarui\|"))
    app.add_handler(CallbackQueryHandler(callback_wishlist, pattern=r"^wish\|"))
    app.add_handler(CallbackQueryHandler(callback_loja, pattern=r"^loja\|"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler_pesquisas_e_venda))
    app.add_handler(MessageHandler(filters.PHOTO | filters.ANIMATION | filters.Document.ALL, evento_midia_handler))
    # Listener de quantidade personalizada de giros (group=1 para não conflitar com wizards admin)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler_custom_spin_texto), group=1)
    app.add_handler(MessageHandler(filters.ALL, handler_xp_grupo), group=2)

    async def handle_webhook(request):
        try:
            data = await request.json()
            update = Update.de_json(data, app.bot)
            await app.update_queue.put(update)
        except Exception as e:
            print(f"⚠️ Erro ao processar update no webhook: {e}", flush=True)
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
    print(f"✅ [2/4] Porta {PORT} aberta com sucesso!", flush=True)

    print("🗄️ [3/4] Conectando e verificando banco de dados...", flush=True)
    await asyncio.to_thread(iniciar_banco)
    print("✅ [3/4] Banco de dados pronto!", flush=True)

    await app.initialize()
    await app.start()
    asyncio.create_task(eventos_manager.loop_agendado(app.bot))
    await asyncio.to_thread(repor_giros_diarios)
    asyncio.create_task(loop_reposicao_diaria())

    await app.bot.set_my_commands([
        BotCommand("girar", "Sortear cartas (ex: /girar, /girar 5, /girar tudo)"),
        BotCommand("apelido", "Definir seu apelido único no jogo"),
        BotCommand("perfil", "Ver e editar seu perfil"),
        BotCommand("casar", "Propor casamento a outro jogador"),
        BotCommand("divorciar", "Encerrar seu casamento"),
        BotCommand("comandos", "Ver todos os comandos disponíveis"),
        BotCommand("colecao", "Ver toda a sua coleção"),
        BotCommand("catalogo", "Listar cartas por categoria"),
        BotCommand("artista", "Ver progresso dos 5 tiers de um artista"),
        BotCommand("ranking", "Ver ranking de jogadores de um artista"),
        BotCommand("carta", "Visualizar uma carta pelo ID"),
        BotCommand("trocar", "Propor troca de cartas"),
        BotCommand("doar", "Doar cartas por botões"),
        BotCommand("wishlist", "Editar ou consultar wishlist"),
        BotCommand("loja", "Vender cartas e comprar giros"),
        BotCommand("eventos", "Ver eventos ativos"),
        BotCommand("natalpresente", "Presentear uma carta de Natal"),
        BotCommand("meuid", "Ver seu ID numérico"),
    ])

    await app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook", allowed_updates=Update.ALL_TYPES)
    print(f"✅ [4/4] Bot online e webhook configurado em {WEBHOOK_URL}/webhook", flush=True)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

