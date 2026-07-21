#!/usr/bin/env python3
# Dependências: discord.py, g4f
# Instale: pip install discord.py g4f

import os
import random
import asyncio
import traceback
from datetime import timedelta, datetime
from collections import defaultdict, OrderedDict
from typing import Optional, Tuple

import discord
import g4f
from g4f.Provider import Yqcloud, OperaAria
from discord.ext import commands

# ─── Config ───────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─── Configuráveis via ambiente (tune conforme infra) ────────────────────────
G4F_TIMEOUT = int(os.getenv("G4F_TIMEOUT", "25"))        # segundos por provedor
G4F_CONCURRENCY = int(os.getenv("G4F_CONCURRENCY", "1")) # chamadas concorrentes (baixo para Termux)
MAX_PROVIDER_RETRIES = int(os.getenv("G4F_RETRIES", "0"))
PROVIDER_RETRY_DELAY = float(os.getenv("G4F_RETRY_DELAY", "1"))
ai_semaphore = asyncio.Semaphore(G4F_CONCURRENCY)

# Force local by default on Termux — can be overridden with env FORCE_LOCAL=0
DEFAULT_FORCE_LOCAL = os.getenv("FORCE_LOCAL", "1") == "1"

# ─── Help embed ───────────────────────────────────────────────────────────────

def build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Comandos do Nova Era Bot",
        description="Todos os comandos começam com `!`",
        color=0x5865F2
    )
    embed.add_field(
        name="Gerais",
        value=(
            "`!helpNova` — Mostra esta lista de comandos\n"
            "`!ai [pergunta]` — Conversa com a IA Nova Era\n"
            "`!ai limpar` — Limpa o histórico de conversa do canal\n"
            "`!aimode [local|external|auto]` — Define modo de IA (admin)\n"
            "`!aistatus` — Verifica provedores externos (diagnóstico)\n"
            "`!userinfo [@membro]` — Informações sobre um membro"
        ),
        inline=False
    )
    embed.add_field(
        name="Moderação",
        value=(
            "`!ban @membro [dias] [motivo]` — Bane um membro (dias de mensagens: 0-7)\n"
            "`!kick @membro [motivo]` — Expulsa um membro\n"
            "`!timeout @membro [minutos] [motivo]` — Silencia temporariamente\n"
            "`!warn @membro [motivo]` — Adverte um membro\n"
            "`!warns @membro` — Lista as advertências de um membro\n"
            "`!clear [quantidade] [@membro]` — Apaga mensagens do canal"
        ),
        inline=False
    )
    embed.set_footer(text="Mencione um membro entre colchetes quando aplicável.")
    return embed

# ─── IA (local fast generator + opcional external) ───────────────────────────

BASE_SYSTEM = (
    "Você é Nova Era, um bot de Discord prestativo, amigável e útil. "
    "Responda sempre em português brasileiro de forma natural e concisa."
)

MAX_HISTORY = 1  # reduzido para Termux

# Histórico por canal: channel_id -> list de dicts
conversation_history: dict[int, list[dict]] = defaultdict(list)

# Modo de IA: per-guild override, values: 'local', 'external', 'auto'
ai_mode_by_guild: dict[int, str] = defaultdict(lambda: 'local' if DEFAULT_FORCE_LOCAL else 'auto')

# Simple in-memory LRU cache for recent Q->A to speed repeated requests
class LRUCache:
    def __init__(self, capacity: int = 256):
        self.capacity = capacity
        self.cache: OrderedDict[Tuple[int,str], str] = OrderedDict()

    def get(self, key: Tuple[int,str]) -> Optional[str]:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def put(self, key: Tuple[int,str], value: str):
        self.cache[key] = value
        self.cache.move_to_end(key)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

    def clear(self):
        self.cache.clear()

ai_cache = LRUCache(256)


async def local_ai_generate(channel_id: int, username: str, user_message: str) -> str:
    history = conversation_history[channel_id]

    msg = (user_message or "").strip()
    lower = msg.lower()

    greetings_in = {"olá", "oi", "eai", "ei", "hello", "hey", "opa"}
    if any(tok in lower.split() for tok in greetings_in):
        reply = random.choice([
            f"Olá, {username}! Como posso ajudar você hoje?",
            "Oi! Me conta o que você quer saber",
            "E aí! Em que posso ajudar?"
        ])
    elif "obrigad" in lower or "valeu" in lower:
        reply = random.choice([
            "Por nada!",
            "De nada — se precisar, estou por aqui!",
            "Imagina, fico feliz em ajudar!"
        ])
    elif "ajuda" in lower or "pode me ajudar" in lower:
        reply = "Claro! Diz pra mim o que você precisa que eu tento ajudar."
    elif "como" in lower and ("vai" in lower or "está" in lower):
        reply = random.choice([
            "Estou bem, obrigado por perguntar! Pronto pra ajudar.",
            "Tudo certo por aqui — e você?"
        ])
    elif "por que" in lower or lower.endswith("?") or "?" in msg:
        reply = random.choice([
            "Boa pergunta — não tenho todos os dados aqui, mas posso sugerir uma direção:",
            "Interessante! Pense nisso assim:",
            "Legal essa pergunta. Uma ideia é considerar o seguinte:"
        ])
        if any(k in lower for k in ("erro", "bug", "falha", "exception")):
            reply += " Verifique os logs, tente reproduzir o erro com um caso mínimo e examine o stack trace."
        elif any(k in lower for k in ("config", "token", "apikey", "env", "variável")):
            reply += " Confirme as variáveis de ambiente e privilégios; muitas falhas vêm daí."
        else:
            reply += " Se quiser, me dá mais contexto e eu tento ajudar melhor."
    else:
        short_echo = (msg if len(msg) <= 120 else msg[:117] + "...")
        reply = random.choice([
            f"Entendi: {short_echo}. Pode explicar mais um pouco?",
            f"Você disse: {short_echo}. Uma sugestão seria decompor o problema em partes menores.",
            f"Interessante. Sobre {short_echo}, você já tentou procurar por exemplos ou testar com casos menores?"
        ])

    # Atualiza histórico (mantendo tamanho máximo)
    history.append({"username": username, "content": user_message, "role": "user"})
    history.append({"username": "Nova Era", "content": reply, "role": "bot"})
    if len(history) > MAX_HISTORY:
        del history[:len(history) - MAX_HISTORY]

    return reply


async def _call_g4f_in_thread(messages, provider):
    def sync_call():
        return g4f.ChatCompletion.create(model=g4f.models.default, messages=messages, provider=provider)
    return await asyncio.to_thread(sync_call)


async def ask_ai_external(messages, channel_id: int, username: str, user_message: str) -> Optional[str]:
    # external providers path
    last_error = None
    async with ai_semaphore:
        for provider in (Yqcloud, OperaAria):
            attempt = 0
            while attempt <= MAX_PROVIDER_RETRIES:
                try:
                    reply = await asyncio.wait_for(_call_g4f_in_thread(messages, provider), timeout=G4F_TIMEOUT)
                    reply = (reply or "").strip()
                    if not reply:
                        raise Exception("Resposta vazia do provedor")

                    # update history
                    history = conversation_history[channel_id]
                    history.append({"username": username, "content": user_message, "role": "user"})
                    history.append({"username": "Nova Era", "content": reply, "role": "bot"})
                    if len(history) > MAX_HISTORY:
                        del history[:len(history) - MAX_HISTORY]

                    return reply
                except asyncio.TimeoutError:
                    last_error = Exception(f"Timeout de {G4F_TIMEOUT}s ao chamar {provider.__name__}")
                    print(f"[AVISO] {provider.__name__} timeout (attempt {attempt}).")
                    traceback.print_exc()
                except Exception as e:
                    last_error = e
                    print(f"[AVISO] {provider.__name__} falhou (attempt {attempt}): {e}")
                    traceback.print_exc()

                attempt += 1
                if attempt <= MAX_PROVIDER_RETRIES:
                    await asyncio.sleep(PROVIDER_RETRY_DELAY)
    return None


async def ask_ai(channel_id: int, username: str, user_message: str) -> str:
    # Check cache first
    key = (channel_id, user_message.strip().lower())
    cached = ai_cache.get(key)
    if cached:
        return cached

    # Determine mode for this guild (if DM, use global default)
    mode = 'local'
    try:
        # try to get guild id via conversation context: not available here so caller sets channel_id
        # we keep mode by guild if possible; fallback to default
        mode = ai_mode_by_guild.get(channel_id, ai_mode_by_guild.get(0, 'local'))
    except Exception:
        mode = 'local' if DEFAULT_FORCE_LOCAL else 'auto'

    # If default FORCE_LOCAL then treat 'auto' as 'local' for Termux
    if DEFAULT_FORCE_LOCAL and mode == 'auto':
        mode = 'local'

    # Local only
    if mode == 'local':
        reply = await local_ai_generate(channel_id, username, user_message)
        ai_cache.put(key, reply)
        return reply

    # External only: prepare messages
    messages = [{"role": "system", "content": BASE_SYSTEM}]
    for h in conversation_history[channel_id]:
        role = h.get("role")
        if role == "user":
            messages.append({"role": "user", "content": f"{h['username']}: {h['content']}"})
        else:
            messages.append({"role": "assistant", "content": h.get("content", "")})
    messages.append({"role": "user", "content": f"{username}: {user_message}"})

    # If auto: try external then fallback local
    if mode == 'auto':
        ext = await ask_ai_external(messages, channel_id, username, user_message)
        if ext:
            ai_cache.put(key, ext)
            return ext
        # fallback
        reply = await local_ai_generate(channel_id, username, user_message)
        ai_cache.put(key, reply)
        return reply

    # mode == 'external'
    ext = await ask_ai_external(messages, channel_id, username, user_message)
    if ext:
        ai_cache.put(key, ext)
        return ext

    # final fallback local
    reply = await local_ai_generate(channel_id, username, user_message)
    ai_cache.put(key, reply)
    return reply


def clear_history(channel_id: int):
    conversation_history[channel_id].clear()


# ─── Eventos ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Nova Era online como {bot.user} ({bot.user.id})")


# Apenas processa comandos com prefixo '!'
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)


# ─── Boas-vindas ao entrar no servidor ───────────────────────────────────────

@bot.event
async def on_member_join(member: discord.Member):
    welcome_text = (
        "Bem-vindo à Nova Era.\n\n"
        "Sua jornada começa agora. Participe das nossas noites de jogos, interaja com a comunidade e faça novas amizades.\n\n"
        "Antes de seguir, confira os canais #seja-booster e #leveis・levels para conhecer todas as vantagens e recompensas disponíveis.\n\n"
        "O futuro começa aqui."
    )

    text_to_send = welcome_text  # sem menção
    guild = member.guild

    candidates = ["chat geral", "chat-geral", "geral", "general"]
    target_channel: Optional[discord.TextChannel] = None
    for name in candidates:
        for ch in guild.text_channels:
            if ch.name.lower() == name and ch.permissions_for(guild.me).send_messages:
                target_channel = ch
                break
        if target_channel:
            break

    if not target_channel and guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        target_channel = guild.system_channel

    if not target_channel:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                target_channel = ch
                break

    sent = False
    if target_channel:
        try:
            await target_channel.send(text_to_send)
            sent = True
        except Exception as e:
            print(f"[AVISO] Falha ao enviar boas-vindas em canal {target_channel}: {e}")
            traceback.print_exc()

    if not sent:
        try:
            await member.send(welcome_text)
        except Exception as e:
            print(f"[AVISO] Não foi possível enviar DM ao membro {member}: {e}")
            traceback.print_exc()


# ─── Comandos de prefixo '!' (mantidos) ──────────────────────────────────────

@bot.command(name="helpNova", aliases=["help"]) 
async def help_nova(ctx: commands.Context):
    await ctx.send(embed=build_help_embed())


@bot.command(name="ai")
async def ai_command(ctx: commands.Context, *, args: str = ""):
    if args.strip().lower() == "limpar":
        clear_history(ctx.channel.id)
        ai_cache.clear()
        await ctx.send("Histórico de conversa e cache limpos.")
        return

    if not args.strip():
        await ctx.send(
            "Use `!ai [sua pergunta]` para conversar.\n"
            "Use `!ai limpar` para limpar o histórico."
        )
        return

    # quick cache check
    key = (ctx.channel.id, args.strip().lower())
    cached = ai_cache.get(key)
    if cached:
        await ctx.send(cached if len(cached) <= 1900 else cached[:1900])
        return

    async with ctx.typing():
        try:
            # For guild-based mode, use guild id as key if available
            guild_id = ctx.guild.id if ctx.guild else 0
            # set conversation key to channel id for history
            reply = await ask_ai(ctx.channel.id, ctx.author.name, args)
            # send
            if len(reply) <= 1900:
                embed = discord.Embed(color=0x5865F2)
                embed.set_author(name=f"{ctx.author.display_name} perguntou:")
                embed.description = f"> {args}\n\n{reply}"
                embed.set_footer(text="Nova Era IA")
                await ctx.send(embed=embed)
            else:
                chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
                await ctx.send(f"{ctx.author.display_name} perguntou: {args}\n\n{chunks[0]}")
                for chunk in chunks[1:]:
                    await ctx.send(chunk)
        except Exception as e:
            print(f"[ERRO] comando !ai falhou: {e}")
            traceback.print_exc()
            await ctx.send("A IA está indisponível no momento. Tente novamente.")


@bot.command(name="aimode")
@commands.has_guild_permissions(administrator=True)
async def aimode(ctx: commands.Context, mode: str):
    """Define o modo de IA para este servidor: local, external ou auto."""
    mode = mode.lower()
    if mode not in ("local", "external", "auto"):
        await ctx.send("Modo inválido. Use: local, external ou auto.")
        return
    gid = ctx.guild.id if ctx.guild else 0
    ai_mode_by_guild[gid] = mode
    await ctx.send(f"Modo de IA deste servidor definido para: {mode}")


@bot.command(name="aistatus")
async def aistatus(ctx: commands.Context):
    """Roda um probe rápido nos provedores g4f para diagnóstico."""
    results = []
    for provider in (Yqcloud, OperaAria):
        try:
            await asyncio.wait_for(_call_g4f_in_thread([{"role":"system","content":"ping"}], provider), timeout=G4F_TIMEOUT)
            results.append(f"{provider.__name__}: OK")
        except asyncio.TimeoutError:
            results.append(f"{provider.__name__}: TIMEOUT")
        except Exception as e:
            results.append(f"{provider.__name__}: ERRO ({e})")
    await ctx.send("\n".join(results))


# Moderation and utility commands (ban/kick/timeout/warn/clear/userinfo) kept as before
# ... (kept unchanged for brevity) - copy from previous implementation

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban_command(ctx: commands.Context, membro: discord.Member, dias: int = 0, *, motivo: str = "Sem motivo informado"):
    if not membro.is_bannable():
        await ctx.send("Não consigo banir esse membro (cargo superior ou igual ao meu).")
        return

    dias = max(0, min(dias, 7))
    try:
        await membro.ban(delete_message_days=dias, reason=motivo)
        embed = discord.Embed(title="Membro Banido", color=0xFF0000)
        embed.add_field(name="Membro", value=str(membro), inline=True)
        embed.add_field(name="Motivo", value=motivo, inline=True)
        embed.add_field(name="Moderador", value=str(ctx.author), inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"[ERRO] ao banir: {e}")
        traceback.print_exc()
        await ctx.send("Erro ao banir o membro.")


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick_command(ctx: commands.Context, membro: discord.Member, *, motivo: str = "Sem motivo informado"):
    if not membro.is_kickable():
        await ctx.send("Não consigo expulsar esse membro (cargo superior ou igual ao meu).")
        return

    try:
        await membro.kick(reason=motivo)
        embed = discord.Embed(title="Membro Expulso", color=0xFF8800)
        embed.add_field(name="Membro", value=str(membro), inline=True)
        embed.add_field(name="Motivo", value=motivo, inline=True)
        embed.add_field(name="Moderador", value=str(ctx.author), inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"[ERRO] ao expulsar: {e}")
        traceback.print_exc()
        await ctx.send("Erro ao expulsar o membro.")


@bot.command(name="timeout")
@commands.has_permissions(moderate_members=True)
async def timeout_command(ctx: commands.Context, membro: discord.Member, minutos: int = 10, *, motivo: str = "Sem motivo informado"):
    minutos = max(1, min(minutos, 40320))
    try:
        await membro.timeout(timedelta(minutes=minutos), reason=motivo)
        duracao = f"{minutos // 60}h {minutos % 60}min" if minutos >= 60 else f"{minutos} minutos"
        embed = discord.Embed(title="Membro Silenciado", color=0xFFCC00)
        embed.add_field(name="Membro", value=str(membro), inline=True)
        embed.add_field(name="Duração", value=duracao, inline=True)
        embed.add_field(name="Motivo", value=motivo, inline=False)
        embed.add_field(name="Moderador", value=str(ctx.author), inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"[ERRO] ao aplicar timeout: {e}")
        traceback.print_exc()
        await ctx.send("Erro ao silenciar o membro.")


warn_store: dict[int, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))


@bot.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def warn_command(ctx: commands.Context, membro: discord.Member, *, motivo: str):
    warn_store[ctx.guild.id][membro.id].append({
        "motivo": motivo,
        "data": datetime.now().strftime("%d/%m/%Y"),
        "moderador": str(ctx.author)
    })
    total = len(warn_store[ctx.guild.id][membro.id])

    embed = discord.Embed(title="Advertência Aplicada", color=0xFFA500)
    embed.add_field(name="Membro", value=str(membro), inline=True)
    embed.add_field(name="Total de Avisos", value=str(total), inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.add_field(name="Moderador", value=str(ctx.author), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="warns")
@commands.has_permissions(moderate_members=True)
async def warns_command(ctx: commands.Context, membro: discord.Member):
    avisos = warn_store.get(ctx.guild.id, {}).get(membro.id, [])
    if not avisos:
        await ctx.send(f"{membro} não possui advertências.")
        return

    desc = "\n\n".join(
        f"**{i+1}.** {w['motivo']}\n> Por {w['moderador']} em {w['data']}"
        for i, w in enumerate(avisos)
    )
    embed = discord.Embed(title=f"Advertências de {membro}", description=desc, color=0xFFA500)
    embed.set_footer(text=f"Total: {len(avisos)} advertência(s)")
    await ctx.send(embed=embed)


@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear_command(ctx: commands.Context, quantidade: int = 10, membro: Optional[discord.Member] = None):
    quantidade = max(1, min(quantidade, 100))

    try:
        await ctx.message.delete()
    except Exception:
        pass

    def check(msg):
        return membro is None or msg.author == membro

    try:
        deleted = await ctx.channel.purge(limit=quantidade if membro is None else 200, check=check)
        deleted = deleted[:quantidade]
        msg = await ctx.send(
            f"{len(deleted)} mensagem(s) apagada(s)" + (f" de {membro}" if membro else "") + "."
        )
        await asyncio.sleep(4)
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception as e:
        print(f"[ERRO] ao apagar mensagens: {e}")
        traceback.print_exc()
        await ctx.send("Erro ao apagar mensagens.")


@bot.command(name="userinfo")
async def userinfo_command(ctx: commands.Context, membro: Optional[discord.Member] = None):
    alvo = membro or ctx.author
    avisos = warn_store.get(ctx.guild.id, {}).get(alvo.id, [])

    embed = discord.Embed(title=f"{alvo}", color=0x5865F2)
    embed.set_thumbnail(url=alvo.display_avatar.url)
    embed.add_field(name="ID", value=str(alvo.id), inline=True)
    if hasattr(alvo, "created_at") and getattr(alvo, "created_at", None):
        embed.add_field(name="Conta criada em", value=f"<t:{int(alvo.created_at.timestamp())}:D>", inline=True)

    if isinstance(alvo, discord.Member):
        if alvo.joined_at:
            embed.add_field(name="Entrou no servidor", value=f"<t:{int(alvo.joined_at.timestamp())}:D>", inline=True)
        cargo_top = alvo.top_role.name if alvo.top_role.name != "@everyone" else "Nenhum"
        embed.add_field(name="Cargo mais alto", value=cargo_top, inline=True)
        if alvo.nick:
            embed.add_field(name="Apelido", value=alvo.nick, inline=True)

    embed.add_field(
        name="Advertências",
        value="Nenhuma" if not avisos else f"{len(avisos)} advertência(s)",
        inline=True
    )
    embed.timestamp = discord.utils.utcnow()
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        print("[ERRO] DISCORD_BOT_TOKEN não definido!")
        exit(1)
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"[ERRO] Falha ao iniciar o bot: {e}")
