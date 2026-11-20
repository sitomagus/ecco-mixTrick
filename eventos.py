"""Event tools for Ecco-Mix Trick.

Kept separate from bot.py so seasonal schedules, event gifts and event menus can
grow without mixing their state machine into the main card game handlers.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

TZ = ZoneInfo("America/Sao_Paulo")


def criar_schema_eventos(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS eventos (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            nome TEXT NOT NULL,
            inicio TIMESTAMPTZ,
            fim TIMESTAMPTZ,
            ativo_manual BOOLEAN NOT NULL DEFAULT FALSE,
            modo_teste BOOLEAN NOT NULL DEFAULT FALSE,
            giros_bonus INTEGER NOT NULL DEFAULT 0,
            texto_bonus TEXT NOT NULL DEFAULT '',
            media_bonus_id TEXT,
            media_bonus_tipo TEXT,
            moldura_png BYTEA,
            giros_enviados_em TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS evento_giros_dados (
            evento_id INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            PRIMARY KEY (evento_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS evento_usuarios (
            evento_id INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            desbloqueado_em TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (evento_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS evento_presentes (
            id SERIAL PRIMARY KEY,
            evento_id INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
            remetente_id BIGINT NOT NULL,
            destinatario_id BIGINT NOT NULL,
            carta_id INTEGER NOT NULL REFERENCES cartas(id) ON DELETE CASCADE,
            dedicatoria TEXT NOT NULL DEFAULT '',
            criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE categorias ADD COLUMN IF NOT EXISTS event_id INTEGER REFERENCES eventos(id) ON DELETE SET NULL;
        ALTER TABLE cartas ADD COLUMN IF NOT EXISTS event_id INTEGER REFERENCES eventos(id) ON DELETE SET NULL;
    """)
    cur.execute("""
        INSERT INTO eventos (slug, nome, inicio, fim)
        VALUES ('natal', 'Natal', %s, %s)
        ON CONFLICT (slug) DO NOTHING
    """, (datetime(2026, 12, 24, 0, 0, tzinfo=TZ), datetime(2026, 12, 26, 0, 0, tzinfo=TZ)))
    cur.execute("SELECT id FROM eventos WHERE slug='natal'")
    natal_id = cur.fetchone()[0]
    cur.close()


class GerenciadorEventos:
    def __init__(self, deps):
        self.d = deps

    def _db(self):
        return self.d["get_db"]()

    def _esc(self, valor):
        return self.d["esc"](str(valor))

    def _eh_admin(self, user_id):
        return self.d["eh_admin"](user_id)

    def registrar(self, app):
        app.bot_data["eventos_manager"] = self
        app.add_handler(CommandHandler("eventos", self.cmd_eventos))
        app.add_handler(CommandHandler("evento", self.cmd_evento_admin))
        app.add_handler(CommandHandler("natalpresente", self.cmd_natal_presente))
        app.add_handler(CommandHandler("pular", self.cmd_pular))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r"^evento\|"))
        app.add_handler(CommandHandler("studio_eventos", self.cmd_evento_admin))

    async def cmd_pular(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get("evento_pendente", {}).get("tipo") == "dedicatoria":
            await self._finalizar_presente(update, context, "")
            context.user_data.pop("evento_pendente", None)
        else:
            await update.effective_message.reply_text("Não há etapa opcional para pular.")

    def _publico(self, evento):
        agora = datetime.now(TZ)
        inicio, fim, manual = evento[3], evento[4], evento[5]
        agendado = bool(inicio and fim and inicio <= agora < fim)
        return bool(manual or agendado)

    def _listar_eventos(self):
        conn = self._db()
        cur = conn.cursor()
        cur.execute("SELECT id, slug, nome, inicio, fim, ativo_manual, modo_teste, giros_bonus, texto_bonus, media_bonus_id, media_bonus_tipo, moldura_png, giros_enviados_em FROM eventos ORDER BY id")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows

    async def cmd_eventos(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        eventos = [e for e in self._listar_eventos() if self._publico(e)]
        botoes = [[InlineKeyboardButton(f"🎉 {e[2]}", callback_data=f"evento|abrir|{e[0]}|{user.id}")] for e in eventos]
        if self._eh_admin(user.id):
            botoes.append([InlineKeyboardButton("⚙️ Gerenciar eventos", callback_data="evento|admin")])
        if not eventos:
            await update.effective_message.reply_text("Não há eventos ativos neste momento.", reply_markup=InlineKeyboardMarkup(botoes) if botoes else None)
        else:
            await update.effective_message.reply_text("🎉 <b>Eventos disponíveis</b>", reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")

    async def cmd_evento_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._eh_admin(update.effective_user.id):
            return
        await self._mostrar_admin(update.effective_message, context)

    async def _mostrar_admin(self, msg, context, editar=False):
        eventos = self._listar_eventos()
        botoes = [[InlineKeyboardButton(f"🎉 {e[2]}", callback_data=f"evento|config|{e[0]}")] for e in eventos]
        botoes.append([InlineKeyboardButton("➕ Criar evento", callback_data="evento|novo")])
        teclado = InlineKeyboardMarkup(botoes)
        texto = "🎛️ <b>Studio de eventos</b>\nEscolha um evento para configurar datas, teste, recompensas e moldura."
        if editar:
            await msg.edit_text(texto, reply_markup=teclado, parse_mode="HTML")
        else:
            await msg.reply_text(texto, reply_markup=teclado, parse_mode="HTML")

    async def _config(self, query, event_id):
        eventos = {e[0]: e for e in self._listar_eventos()}
        e = eventos.get(event_id)
        if not e:
            await query.answer("Evento não encontrado.", show_alert=True)
            return
        ativo = "🟢 ativo manualmente" if e[5] else "⚪ manual desligado"
        teste = "🧪 teste ligado" if e[6] else "teste desligado"
        horario = f"{e[3]} → {e[4]}" if e[3] and e[4] else "sem agenda automática"
        botoes = [
            [InlineKeyboardButton("🧪 Abrir prévia de teste", callback_data=f"evento|teste_menu|{event_id}")],
            [InlineKeyboardButton("🧪 Abrir prévia de teste", callback_data=f"evento|teste_menu|{event_id}")],
            [InlineKeyboardButton(f"Alternar ativação ({ativo})", callback_data=f"evento|ativar|{event_id}")],
            [InlineKeyboardButton(f"Alternar modo de teste ({teste})", callback_data=f"evento|teste|{event_id}")],
            [InlineKeyboardButton("🗓️ Configurar agenda", callback_data=f"evento|agenda|{event_id}")],
            [InlineKeyboardButton(f"🎲 Giros para todos ({e[7]})", callback_data=f"evento|qtd|{event_id}")],
            [InlineKeyboardButton("📝 Texto da recompensa", callback_data=f"evento|texto|{event_id}"), InlineKeyboardButton("🖼️ Mídia da recompensa", callback_data=f"evento|midia|{event_id}")],
            [InlineKeyboardButton("🖼️ Moldura do evento", callback_data=f"evento|moldura|{event_id}")],
            [InlineKeyboardButton("🎁 Distribuir giros agora", callback_data=f"evento|distribuir|{event_id}")],
            [InlineKeyboardButton("📂 Abrir categoria de cartas", callback_data=f"evento|categoria|{event_id}")],
            [InlineKeyboardButton("🔙 Lista de eventos", callback_data="evento|admin")],
        ]
        botoes.insert(-1,[InlineKeyboardButton("➕ Criar cartas deste evento",callback_data=f"studio_go|addevent|{event_id}")])
        await query.answer()
        await query.edit_message_text(f"🎉 <b>{self._esc(e[2])}</b>\nAgenda: {self._esc(horario)}\nEstado: {ativo}; {teste}", reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")

    async def _menu_evento(self, query, event_id, user_id, teste=False):
        if query.from_user.id != user_id:
            await query.answer("Esse menu pertence a outra pessoa.", show_alert=True)
            return
        eventos = {e[0]: e for e in self._listar_eventos()}
        e = eventos.get(event_id)
        if not e or (not self._publico(e) and not (teste and self._eh_admin(user_id) and e[6])):
            await query.answer("Este evento não está ativo.", show_alert=True)
            return
        if not teste and not self._eh_admin(user_id):
            conn = self._db(); cur = conn.cursor()
            cur.execute("SELECT 1 FROM evento_usuarios WHERE evento_id=%s AND user_id=%s", (event_id, user_id))
            liberado = cur.fetchone(); cur.close(); conn.close()
            if not liberado:
                await query.answer("Você precisa receber ou enviar um presente deste evento para desbloquear o menu.", show_alert=True)
                return
        conn = self._db()
        cur = conn.cursor()
        cur.execute("SELECT id, nome FROM categorias WHERE event_id IS NULL ORDER BY nome")
        categorias = cur.fetchall()
        cur.close()
        conn.close()
        botoes = [[InlineKeyboardButton(nome, callback_data=f"evento|cat|{event_id}|{cat_id}|{user_id}|{int(teste)}")] for cat_id, nome in categorias]
        if self._eh_admin(user_id):
            botoes.append([InlineKeyboardButton("➕ Criar cartas deste evento",callback_data=f"studio_go|addevent|{event_id}")])
        botoes.append([InlineKeyboardButton("🌐 Girar no evento inteiro", callback_data=f"evento|quant|{event_id}|0|0|{user_id}|{int(teste)}")])
        botoes.append([InlineKeyboardButton("🔙 Eventos", callback_data="evento|lista")])
        await query.answer()
        await query.edit_message_text(f"🎉 <b>{self._esc(e[2])}</b>\nEscolha categoria ou gire pelo evento inteiro:", reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")

    async def _quantidades(self, query, event_id, cat_id, sub_id, user_id, teste=False):
        botoes = []
        for qtd in (1, 3, 5, 10):
            botoes.append([InlineKeyboardButton(f"🎲 {qtd} giro(s)", callback_data=f"evento|girar|{event_id}|{cat_id}|{sub_id}|{qtd}|{user_id}|{int(teste)}")])
        botoes.append([InlineKeyboardButton("🌟 Todos os giros", callback_data=f"evento|girar|{event_id}|{cat_id}|{sub_id}|all|{user_id}|{int(teste)}")])
        await query.answer()
        await query.edit_message_text("Quantos giros deseja fazer?", reply_markup=InlineKeyboardMarkup(botoes))

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        p = q.data.split("|")
        acao = p[1]
        if acao == "admin":
            if not self._eh_admin(q.from_user.id):
                await q.answer("Apenas administradores.", show_alert=True); return
            await q.answer(); await self._mostrar_admin(q.message, context, editar=True); return
        if acao == "lista":
            await self.cmd_eventos(update, context); return
        if acao == "config":
            if not self._eh_admin(q.from_user.id):
                await q.answer("Apenas administradores.", show_alert=True); return
            await self._config(q, int(p[2])); return
        if acao == "novo":
            if not self._eh_admin(q.from_user.id):
                await q.answer("Apenas administradores.", show_alert=True); return
            context.user_data["evento_pendente"] = {"tipo": "novo"}
            await q.answer(); await q.edit_message_text("Digite o nome do evento. As categorias e subcategorias do catálogo normal ficam disponíveis automaticamente para cadastrar as cartas do evento."); return
        if acao in ("ativar", "teste"):
            if not self._eh_admin(q.from_user.id):
                await q.answer("Apenas administradores.", show_alert=True); return
            eid = int(p[2]); coluna = "ativo_manual" if acao == "ativar" else "modo_teste"
            conn = self._db(); cur = conn.cursor(); cur.execute(f"UPDATE eventos SET {coluna}=NOT {coluna} WHERE id=%s", (eid,)); conn.commit(); cur.close(); conn.close()
            await self._config(q, eid); return
        if acao in ("agenda", "qtd", "texto", "midia", "moldura"):
            if not self._eh_admin(q.from_user.id):
                await q.answer("Apenas administradores.", show_alert=True); return
            eid = int(p[2]); context.user_data["evento_pendente"] = {"tipo": acao, "id": eid}
            await q.answer()
            prompts = {
                "agenda": "Envie início e fim no formato <code>DD/MM/AAAA HH:MM | DD/MM/AAAA HH:MM</code> (horário de São Paulo).",
                "qtd": "Digite quantos giros cada jogador deve receber ao distribuir a recompensa (0 para desativar).",
                "texto": "Digite o texto que acompanhará a recompensa de giros.",
                "midia": "Envie uma foto ou GIF para acompanhar a recompensa. Envie /pular para remover.",
                "moldura": "Envie a moldura PNG como arquivo para este evento.",
            }
            await q.edit_message_text(prompts[acao], parse_mode="HTML"); return
        if acao == "distribuir":
            if not self._eh_admin(q.from_user.id):
                await q.answer("Apenas administradores.", show_alert=True); return
            await self.distribuir_giros(int(p[2]), context.bot)
            await q.answer("Recompensa distribuída uma vez para cada jogador cadastrado.", show_alert=True)
            await self._config(q, int(p[2])); return
        if acao == "categoria":
            if not self._eh_admin(q.from_user.id):
                await q.answer("Apenas administradores.", show_alert=True); return
            event_id = int(p[2]); conn = self._db(); cur = conn.cursor()
            cur.execute("SELECT id,nome FROM categorias WHERE event_id IS NULL ORDER BY nome"); rows = cur.fetchall()
            cur.close(); conn.close()
            if not rows:
                await q.answer("Cadastre categorias no catálogo primeiro.",show_alert=True); return
            buttons=[[InlineKeyboardButton(nome,callback_data=f"evento|cat|{event_id}|{cat_id}|{q.from_user.id}|0")] for cat_id,nome in rows]
            buttons.append([InlineKeyboardButton("➕ Criar cartas deste evento",callback_data=f"studio_go|addevent|{event_id}")])
            await q.answer(); await q.edit_message_text("Escolha a categoria do catálogo para conferir as cartas deste evento:",reply_markup=InlineKeyboardMarkup(buttons)); return
        if acao == "abrir":
            await self._menu_evento(q, int(p[2]), int(p[3]), bool(int(p[4])) if len(p) > 4 else False); return
        if acao == "cat":
            await self._mostrar_subcategorias(q, int(p[2]), int(p[3]), int(p[4]), bool(int(p[5]))); return
        if acao == "sub":
            await self._quantidades(q, int(p[2]), int(p[3]), int(p[4]), int(p[5]), bool(int(p[6]))); return
        if acao == "quant":
            await self._quantidades(q, int(p[2]), int(p[3]), int(p[4]), int(p[5]), bool(int(p[6]))); return
        if acao == "girar":
            eid, cat, sub, qtd, uid, teste = int(p[2]), int(p[3]) or None, int(p[4]) or None, p[5], int(p[6]), bool(int(p[7]))
            if q.from_user.id != uid:
                await q.answer("Esse menu pertence a outra pessoa.", show_alert=True); return
            if teste:
                if not self._eh_admin(uid):
                    await q.answer("Modo de teste restrito a administradores.", show_alert=True); return
                await self._preview_teste(q, eid, cat, sub); return
            qtd_num = self.d["verificar_usuario"](uid, q.from_user.username) if qtd == "all" else int(qtd)
            await self.d["executar_sorteio"](q, context, cat_id=cat, sub_id=sub, subsub_id=None, qtd_giros=qtd_num, event_id=eid); return
        if acao == "teste_menu":
            if not self._eh_admin(q.from_user.id):
                await q.answer("Apenas administradores.", show_alert=True); return
            await self._menu_evento(q, int(p[2]), q.from_user.id, teste=True); return
        if acao == "giftcard":
            await self._gift_quantidades(q, context, int(p[2]), int(p[3]), int(p[4])); return
        if acao == "giftqty":
            await self._gift_confirm(q, context, int(p[2]), int(p[3]), int(p[4]), int(p[5])); return

    async def _mostrar_subcategorias(self, q, event_id, cat_id, user_id, teste=False):
        if q.from_user.id != user_id:
            await q.answer("Esse menu pertence a outra pessoa.", show_alert=True); return
        conn = self._db(); cur = conn.cursor()
        cur.execute("SELECT id,nome FROM subcategorias WHERE categoria_id=%s ORDER BY nome", (cat_id,)); subs = cur.fetchall()
        cur.close(); conn.close()
        botoes = [[InlineKeyboardButton(nome, callback_data=f"evento|sub|{event_id}|{cat_id}|{sid}|{user_id}|{int(teste)}")] for sid, nome in subs]
        botoes.append([InlineKeyboardButton("🌐 Toda a categoria", callback_data=f"evento|quant|{event_id}|{cat_id}|0|{user_id}|{int(teste)}")])
        botoes.append([InlineKeyboardButton("🔙 Voltar ao evento", callback_data=f"evento|abrir|{event_id}|{user_id}|{int(teste)}")])
        await q.answer(); await q.edit_message_text("Escolha uma subcategoria ou toda a categoria:", reply_markup=InlineKeyboardMarkup(botoes))

    async def _preview_teste(self, q, event_id, cat_id, sub_id):
        conn = self._db(); cur = conn.cursor()
        sql = "SELECT c.id,c.nome,c.tier,c.file_id,c.media_type FROM cartas c WHERE c.event_id=%s"
        params = [event_id]
        if cat_id:
            sql += " AND c.categoria_id=%s"; params.append(cat_id)
        if sub_id:
            sql += " AND c.subcategoria_id=%s"; params.append(sub_id)
        cur.execute(sql + " ORDER BY RANDOM() LIMIT 1", tuple(params)); row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            await q.answer("Nenhuma carta de evento nessa seleção.", show_alert=True); return
        await q.answer("Prévia de teste — nenhum giro foi consumido.")
        texto = f"🧪 <b>Teste do evento</b>\nCarta de exemplo: <b>{self._esc(row[1])}</b> • {self._esc(self.d['tier_nome'](row[2]))}"
        tags=self.d.get("tags_por_carta",lambda ids:{})([row[0]]).get(row[0],[])
        if tags: texto += "\n🏷️ <b>Tags:</b> " + ", ".join(f"#{self._esc(tag)}" for tag in tags)
        if row[4] == "animation":
            await q.message.reply_animation(row[3], caption=texto, parse_mode="HTML")
        else:
            await q.message.reply_photo(row[3], caption=texto, parse_mode="HTML")

    async def cmd_natal_presente(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user; msg = update.effective_message
        if not user or not msg:
            return
        alvo_txt = " ".join(context.args).strip()
        if not alvo_txt:
            await msg.reply_text("Use <code>/natalpresente @pessoa</code> ou responda à mensagem dela com <code>/natalpresente</code>.", parse_mode="HTML"); return
        alvo = self.d["resolver_jogador"](alvo_txt)
        if not alvo:
            await msg.reply_text("Não encontrei esse jogador; ele precisa ter iniciado o bot."); return
        target_id, username, alias = alvo
        if target_id == user.id:
            await msg.reply_text("Escolha outra pessoa para presentear."); return
        eventos = [e for e in self._listar_eventos() if e[1] == "natal" and self._publico(e)]
        if not eventos:
            await msg.reply_text("O evento de Natal não está ativo neste momento."); return
        eid = eventos[0][0]
        conn = self._db(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM evento_presentes WHERE evento_id=%s AND destinatario_id=%s", (eid, target_id)); recebidos = cur.fetchone()[0]
        restante = max(0, 3 - recebidos)
        cur.execute("SELECT c.id,c.nome,c.tier,c.file_id,c.media_type,SUM(i.quantidade) FROM cartas c JOIN inventario i ON i.carta_id=c.id WHERE c.event_id=%s AND i.user_id=%s AND i.quantidade>0 GROUP BY c.id,c.nome,c.tier,c.file_id,c.media_type ORDER BY c.nome,c.id", (eid, user.id))
        cartas = cur.fetchall(); cur.close(); conn.close()
        if restante <= 0:
            await msg.reply_text("Essa pessoa já recebeu o limite de 3 cartas de Natal."); return
        if not cartas:
            await msg.reply_text("Você não tem cartas de Natal para presentear."); return
        context.user_data["natal_presente"] = {"event_id": eid, "from": user.id, "to": target_id, "to_name": alias or (f"@{username}" if username else str(target_id)), "chat_id": msg.chat_id}
        botoes = [[InlineKeyboardButton(f"#{cid} {nome[:28]} ×{qtd}", callback_data=f"evento|giftcard|{eid}|{target_id}|{cid}")] for cid,nome,tier,file_id,media,qtd in cartas]
        await msg.reply_text(f"🎁 Escolha uma carta de Natal para {self._esc(context.user_data['natal_presente']['to_name'])}. Essa pessoa pode receber mais {restante}.", reply_markup=InlineKeyboardMarkup(botoes), parse_mode="HTML")

    async def _gift_quantidades(self, q, context, event_id, target_id, card_id):
        flow = context.user_data.get("natal_presente")
        if not flow or flow.get("from") != q.from_user.id or flow.get("to") != target_id:
            await q.answer("Esse fluxo de presente expirou.", show_alert=True); return
        conn=self._db(); cur=conn.cursor()
        cur.execute("SELECT nome,file_id,media_type FROM cartas WHERE id=%s AND event_id=%s", (card_id,event_id)); card=cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(quantidade),0) FROM inventario WHERE user_id=%s AND carta_id=%s", (q.from_user.id,card_id)); owned=cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM evento_presentes WHERE evento_id=%s AND destinatario_id=%s", (event_id,target_id)); got=cur.fetchone()[0]
        cur.close(); conn.close()
        max_qty=min(owned, max(0,3-got))
        if not card or max_qty<1:
            await q.answer("Sem cópias disponíveis ou limite de presentes atingido.", show_alert=True); return
        flow["card_id"]=card_id; flow["card_name"]=card[0]
        buttons=[[InlineKeyboardButton(f"🎁 {n}", callback_data=f"evento|giftqty|{event_id}|{target_id}|{card_id}|{n}")] for n in range(1,max_qty+1)]
        await q.answer(); await q.edit_message_text(f"Quantas cópias de <b>{self._esc(card[0])}</b> vai presentear?", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    async def _gift_confirm(self, q, context, event_id, target_id, card_id, quantity):
        flow=context.user_data.get("natal_presente")
        if not flow or flow.get("from")!=q.from_user.id:
            await q.answer("Esse fluxo expirou.", show_alert=True); return
        flow["quantity"]=quantity
        context.user_data["evento_pendente"]={"tipo":"dedicatoria","id":event_id}
        await q.answer(); await q.edit_message_text("Escreva uma mensagem curta para acompanhar o presente (máximo 250 caracteres). Use /pular para enviar sem texto.")

    async def processar_texto(self, update, context):
        pending=context.user_data.get("evento_pendente")
        if not pending:
            return False
        msg=update.effective_message
        tipo=pending.get("tipo"); eid=pending.get("id")
        if tipo == "novo":
            nome=msg.text.strip()
            if not nome:
                await msg.reply_text("Digite um nome para o evento."); return True
            slug="".join(ch.lower() if ch.isalnum() else "_" for ch in nome).strip("_")[:48] or "evento"
            conn=self._db(); cur=conn.cursor(); cur.execute("INSERT INTO eventos(slug,nome) VALUES(%s,%s) ON CONFLICT(slug) DO UPDATE SET nome=EXCLUDED.nome RETURNING id", (slug,nome)); event_id=cur.fetchone()[0]
            conn.commit(); cur.close(); conn.close()
            context.user_data.pop("evento_pendente",None)
            await msg.reply_text(f"Evento <b>{self._esc(nome)}</b> criado. Ele já usa as categorias e subcategorias existentes no catálogo.\nAtalhos: <code>/addcarta-{slug}</code> ou <code>/addcarta_{slug}</code>.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Criar cartas do evento",callback_data=f"studio_go|addevent|{event_id}")],[InlineKeyboardButton("⚙️ Configurar evento",callback_data=f"evento|config|{event_id}")]]),parse_mode="HTML"); return True
        if tipo == "agenda":
            try:
                left,right=[x.strip() for x in msg.text.split("|")]
                start=datetime.strptime(left,"%d/%m/%Y %H:%M").replace(tzinfo=TZ)
                end=datetime.strptime(right,"%d/%m/%Y %H:%M").replace(tzinfo=TZ)
                if end<=start: raise ValueError
            except Exception:
                await msg.reply_text("Formato inválido. Exemplo: <code>24/12/2026 00:00 | 26/12/2026 00:00</code>",parse_mode="HTML"); return True
            conn=self._db(); cur=conn.cursor(); cur.execute("UPDATE eventos SET inicio=%s,fim=%s WHERE id=%s",(start,end,eid)); conn.commit(); cur.close(); conn.close()
        elif tipo == "qtd":
            try: qty=max(0,int(msg.text.strip()))
            except ValueError: await msg.reply_text("Digite um inteiro de giros (0 ou mais)."); return True
            conn=self._db(); cur=conn.cursor(); cur.execute("UPDATE eventos SET giros_bonus=%s WHERE id=%s",(qty,eid)); conn.commit(); cur.close(); conn.close()
        elif tipo == "texto":
            conn=self._db(); cur=conn.cursor(); cur.execute("UPDATE eventos SET texto_bonus=%s WHERE id=%s",(msg.text[:3000],eid)); conn.commit(); cur.close(); conn.close()
        elif tipo == "dedicatoria":
            dedication="" if (msg.text or "").strip()=="/pular" else (msg.text or "").strip()
            if len(dedication)>250:
                await msg.reply_text("A mensagem pode ter até 250 caracteres."); return True
            await self._finalizar_presente(update,context,dedication)
            return True
        context.user_data.pop("evento_pendente",None)
        await msg.reply_text("Configuração salva. Abra /eventos para continuar.")
        return True

    async def processar_midia(self, update, context):
        pending=context.user_data.get("evento_pendente")
        if not pending or pending.get("tipo") not in ("midia","moldura"):
            return False
        msg=update.effective_message; eid=pending["id"]
        if pending["tipo"]=="midia":
            if msg.animation: file_id,media_type=msg.animation.file_id,"animation"
            elif msg.photo: file_id,media_type=msg.photo[-1].file_id,"photo"
            else: await msg.reply_text("Envie uma foto ou GIF."); return True
            sql="UPDATE eventos SET media_bonus_id=%s,media_bonus_tipo=%s WHERE id=%s"; params=(file_id,media_type,eid)
        else:
            if not msg.document or not msg.document.mime_type or "png" not in msg.document.mime_type.lower():
                await msg.reply_text("Envie a moldura como arquivo PNG."); return True
            tg=await context.bot.get_file(msg.document.file_id); buf=__import__("io").BytesIO(); await tg.download_to_memory(buf)
            moldura_bytes=buf.getvalue()
            sql="UPDATE eventos SET moldura_png=%s WHERE id=%s"; params=(moldura_bytes,eid)
        conn=self._db(); cur=conn.cursor(); cur.execute(sql,params); conn.commit(); cur.close(); conn.close()
        if pending["tipo"] == "moldura":
            total = await self.d["regenerar_moldura_evento"](context, eid, moldura_bytes, msg.chat_id)
            await msg.reply_text(f"Moldura aplicada às cartas existentes do evento ({total} atualizadas).")
        context.user_data.pop("evento_pendente",None)
        await msg.reply_text("Mídia/configuração do evento salva.")
        return True

    async def _finalizar_presente(self, update, context, dedication):
        flow=context.user_data.get("natal_presente")
        if not flow:
            await update.effective_message.reply_text("Fluxo de presente expirou."); return
        eid,giver,target,card_id,qty=flow["event_id"],flow["from"],flow["to"],flow["card_id"],flow["quantity"]
        conn=self._db(); cur=conn.cursor()
        cur.execute("SELECT user_id FROM usuarios WHERE user_id=%s FOR UPDATE", (target,)); cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM evento_presentes WHERE evento_id=%s AND destinatario_id=%s",(eid,target)); received=cur.fetchone()[0]
        if received+qty>3:
            conn.rollback(); cur.close(); conn.close(); context.user_data.pop("natal_presente",None); await update.effective_message.reply_text("A pessoa já atingiu o limite de 3 cartas de Natal."); return
        if not self.d["retirar_inventario"](cur,giver,card_id,qty):
            conn.rollback(); cur.close(); conn.close(); await update.effective_message.reply_text("Você não tem mais essas cópias."); return
        self.d["adicionar_inventario"](cur,target,card_id,qty)
        for _ in range(qty):
            cur.execute("INSERT INTO evento_presentes(evento_id,remetente_id,destinatario_id,carta_id,dedicatoria) VALUES(%s,%s,%s,%s,%s)",(eid,giver,target,card_id,dedication))
        cur.execute("INSERT INTO evento_usuarios(evento_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(eid,target))
        cur.execute("INSERT INTO evento_usuarios(evento_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(eid,giver))
        cur.execute("SELECT nome,file_id,media_type FROM cartas WHERE id=%s",(card_id,)); card=cur.fetchone()
        cur.execute("SELECT nome,apelido,username FROM usuarios WHERE user_id=%s",(giver,)); sender=cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        sender_name=(sender[1] or ("@"+sender[2] if sender[2] else f"Mixer #{str(giver)[-4:]}")) if sender else "Alguém"
        caption=f"🎄 <b>Você recebeu um presente de Natal!</b>\n{qty}× <b>{self._esc(card[0])}</b> de <b>{self._esc(sender_name)}</b>"
        if dedication: caption += f"\n\n💌 {self._esc(dedication)}"
        buttons=InlineKeyboardMarkup([[InlineKeyboardButton("🎄 Abrir evento de Natal",callback_data=f"evento|abrir|{eid}|{target}")]])
        try:
            if card[2]=="animation": await context.bot.send_animation(chat_id=target,animation=card[1],caption=caption,parse_mode="HTML",reply_markup=buttons)
            else: await context.bot.send_photo(chat_id=target,photo=card[1],caption=caption,parse_mode="HTML",reply_markup=buttons)
        except Exception:
            await update.effective_message.reply_text("Presente registrado, mas não consegui avisar no privado. A pessoa precisa abrir o bot com /start.")
        context.user_data.pop("natal_presente",None)
        await update.effective_message.reply_text("🎁 Presente de Natal enviado.")
        try:
            await context.bot.send_message(chat_id=giver, text=f"🎁 Você presenteou {qty}x <b>{self._esc(card[0])}</b> para {self._esc(flow.get('to_name', 'jogador'))}.", parse_mode="HTML")
        except Exception:
            pass
        if flow.get("chat_id"):
            try:
                await context.bot.send_message(chat_id=flow["chat_id"], text=f"🎁 {self._esc(sender_name)} presenteou {qty}x <b>{self._esc(card[0])}</b> para {self._esc(flow.get('to_name', 'jogador'))}.", parse_mode="HTML")
            except Exception:
                pass

    async def distribuir_giros(self,event_id,bot):
        conn=self._db(); cur=conn.cursor()
        cur.execute("SELECT giros_bonus,texto_bonus,media_bonus_id,media_bonus_tipo,nome FROM eventos WHERE id=%s FOR UPDATE",(event_id,)); event=cur.fetchone()
        if not event or event[0]<=0:
            conn.rollback(); cur.close(); conn.close(); return
        cur.execute("SELECT user_id,giros_restantes FROM usuarios WHERE user_id NOT IN (SELECT user_id FROM evento_giros_dados WHERE evento_id=%s) ORDER BY user_id",(event_id,)); users=cur.fetchall()
        for uid,giros in users:
            cur.execute("UPDATE usuarios SET giros_restantes=giros_restantes+%s WHERE user_id=%s",(event[0],uid))
            cur.execute("INSERT INTO evento_giros_dados(evento_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(event_id,uid))
            cur.execute("INSERT INTO evento_usuarios(evento_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(event_id,uid))
        cur.execute("UPDATE eventos SET giros_enviados_em=NOW() WHERE id=%s",(event_id,)); conn.commit(); cur.close(); conn.close()
        for uid,_ in users:
            texto=f"🎉 <b>{self._esc(event[4])}: +{event[0]} giros!</b>"
            if event[1]: texto += "\n\n"+self._esc(event[1])
            try:
                if event[2] and event[3]=="animation": await bot.send_animation(chat_id=uid,animation=event[2],caption=texto,parse_mode="HTML")
                elif event[2]: await bot.send_photo(chat_id=uid,photo=event[2],caption=texto,parse_mode="HTML")
                else: await bot.send_message(chat_id=uid,text=texto,parse_mode="HTML")
            except Exception: pass

    async def loop_agendado(self, bot):
        while True:
            try:
                now=datetime.now(TZ)
                conn=self._db(); cur=conn.cursor()
                cur.execute("SELECT id FROM eventos WHERE inicio<=%s AND fim>%s AND giros_bonus>0 AND giros_enviados_em IS NULL",(now,now))
                event_ids=[r[0] for r in cur.fetchall()]; cur.close(); conn.close()
                for event_id in event_ids: await self.distribuir_giros(event_id,bot)
            except Exception as exc:
                print(f"Erro na rotina de eventos: {exc}",flush=True)
            await __import__("asyncio").sleep(60)


