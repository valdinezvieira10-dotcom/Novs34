#!/usr/bin/env python3
# main.py
# Dependências: discord.py, aiohttp, g4f
# Instale: pip install discord.py aiohttp g4f

import os
import random
import asyncio
from datetime import timedelta
from collections import defaultdict

import aiohttp
import discord
import g4f
from g4f.Provider import Yqcloud, OperaAria
from discord.ext import commands
from discord import app_commands

# ─── Config ───────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ─── IA (g4f + fallback local) ────────────────────────────────────────────────

BASE_SYSTEM = (
    "Você é Nova Era, um bot de Discord prestativo, divertido e amigável. "
    "Responda sempre em português brasileiro de forma natural e engajante. "
    "Seja conciso mas útil. Não use markdown com asteriscos — use texto simples no Discord."
)

MAX_HISTORY = 8
MAX_RETRIES = 2
RETRY_DELAY = 2  # segundos
G4F_TIMEOUT = 25  # segundos para cada provedor

# Histórico por canal: channel_id -> list de dicts
conversation_history: dict[int, list[dict]] = defaultdict(list)


async def local_ai_generate(channel_id: int, username: str, user_message: str) -> str:
    """
    Gera respostas simples sem usar serviços externos.
    Não requer chave de API.
    """
    history = conversation_history[channel_id]

    msg = (user_message or "").strip()
    lower = msg.lower()

    greetings_in = {"olá", "oi", "eai", "ei", "hello", "hey", "opa"}
    if any(tok in lower.split() for tok in greetings_in):
        reply = random.choice([
            f"Olá, {username}! Como posso ajudar você hoje?",
            "Oi! Me conta o que você quer saber 😊",
            "E aí! Em que posso ajudar?"
        ])

    elif "obrigad" in lower or "valeu" in lower:
        reply = random.choice([
            "Por nada! 😊",
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
        suggestions = [
            f"Entendi: {short_echo}. Pode explicar mais um pouco?",
            f"Você disse: {short_echo}. Uma sugestão seria decompor o problema em partes menores.",
            f"Interessante. Sobre {short_echo}, você já tentou procurar por exemplos ou testar com casos menores?"
        ]
        reply = random.choice(suggestions)

    # Atualiza histórico com user + bot
    history.append({"username": username, "content": user_message, "role": "user"})
    history.append({"username": "Nova Era", "content": reply, "role": "bot"})
    if len(history) > MAX_HISTORY:
        del history[:len(history) - MAX_HISTORY]

    return reply


async def _call_g4f_in_thread(messages, provider):
    """
    Chama g4f.ChatCompletion.create de forma síncrona dentro de thread para não bloquear o loop.
    Retorna string resposta ou levanta exceção.
    """
    def sync_call():
        # usa o modelo default do g4f
        return g4f.ChatCompletion.create(model=g4f.models.default, messages=messages, provider=provider)

    # executa em thread e retorna
    return await asyncio.to_thread(sync_call)


async def ask_ai(channel_id: int, username: str, user_message: str) -> str:
    """
    Usa g4f (Yqcloud / OperaAria) sem precisar de API key.
    Fallback para IA local se ambos falharem ou se FORCEL_LOCAL=1 estiver definido.
    """
    if os.environ.get("FORCE_LOCAL", "") == "1":
        return await local_ai_generate(channel_id, username, user_message)

    history = conversation_history[channel_id]

    # monta mensagens no estilo chat (system + histórico + user)
    messages = [{"role": "system", "content": BASE_SYSTEM}]
    for h in history:
        if h["role"] == "user":
            messages.append({"role": "user", "content": f"{h['username']}: {h['content']}"})
        else:
            messages.append({"role": "assistant", "content": h["content"]})
    messages.append({"role": "user", "content": f"{username}: {user_message}"})

    last_error = None
    # tenta provedores na ordem
    for provider in (Yqcloud, OperaAria):
        try:
            # chama a função de forma não-bloqueante com timeout
            reply = await asyncio.wait_for(_call_g4f_in_thread(messages, provider), timeout=G4F_TIMEOUT)
            reply = (reply or "").strip()
            if not reply:
                raise Exception("Resposta vazia do provedor")

            # atualiza histórico
            history.append({"username": username, "content": user_message, "role": "user"})
            history.append({"username": "Nova Era", "content": reply, "role": "bot"})
            if len(history) > MAX_HISTORY:
                del history[:len(history) - MAX_HISTORY]

            return reply
        except asyncio.TimeoutError:
            last_error = Exception(f"Timeout de {G4F_TIMEOUT}s ao chamar {provider.__name__}")
            print(f"[AVISO] {provider.__name__} timeout.")
        except Exception as e:
            last_error = e
            print(f"[AVISO] {provider.__name__} falhou: {e}")
        # pequena espera antes de tentar o próximo provedor
        await asyncio.sleep(1)

    # se chegou aqui, todos os provedores falharam -> fallback local
    print("[INFO] Todos provedores g4f falharam — usando IA local como fallback.")
    try:
        return await local_ai_generate(channel_id, username, user_message)
    except Exception as e:
        print(f"[ERRO] IA local também falhou: {e}")
        raise last_error or e or Exception("IA indisponível")


def clear_history(channel_id: int):
    conversation_history[channel_id].clear()


# ─── Eventos ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[OK] Nova Era online como {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"[OK] {len(synced)} comandos slash registrados")
    except Exception as e:
        print(f"[ERRO] Falha ao registrar comandos: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_mention = bot.user in message.mentions
    content_lower = message.content.lower()
    tokens = content_lower.replace(",", " ").replace("!", " ").split()
    has_nova = "nova" in tokens

    if is_mention or has_nova:
        content = message.content
        if bot.user:
            content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

        if not content:
            await message.reply("Olá! 👋 Me manda uma mensagem ou usa `/ai` para conversar comigo!")
            return

        async with message.channel.typing():
            try:
                resposta = await ask_ai(message.channel.id, message.author.name, content)
                if len(resposta) <= 1900:
                    await message.reply(resposta)
                else:
                    chunks = [resposta[i:i+1900] for i in range(0, len(resposta), 1900)]
                    await message.reply(chunks[0])
                    for chunk in chunks[1:]:
                        await message.channel.send(chunk)
            except Exception as e:
                print(f"[ERRO] IA falhou: {e}")
                await message.reply("❌ A IA está indisponível no momento. Tenta de novo!")

    await bot.process_commands(message)


# ─── Comando /ai ──────────────────────────────────────────────────────────────

@bot.tree.command(name="ai", description="Converse com a IA Nova Era")
@app_commands.describe(
    pergunta="O que você quer perguntar?",
    limpar="Limpa o histórico de conversa deste canal"
)
async def ai_command(interaction: discord.Interaction, pergunta: str = None, limpar: bool = False):
    await interaction.response.defer()

    if limpar:
        clear_history(interaction.channel_id)
        await interaction.followup.send("🧹 Histórico de conversa deste canal foi limpo!")
        return

    if not pergunta:
        await interaction.followup.send(
            "💬 Use `/ai pergunta:Sua mensagem` para conversar.\n"
            "Use `/ai limpar:True` para limpar o histórico."
        )
        return

    try:
        resposta = await ask_ai(interaction.channel_id, interaction.user.name, pergunta)
        embed = discord.Embed(color=0x5865F2)
        embed.set_author(
            name=f"{interaction.user.display_name} perguntou:",
            icon_url=interaction.user.display_avatar.url
        )
        if len(resposta) <= 1900:
            embed.description = f"> {pergunta}\n\n{resposta}"
            embed.set_footer(text="Nova Era IA · g4f")
            await interaction.followup.send(embed=embed)
        else:
            chunks = [resposta[i:i+1900] for i in range(0, len(resposta), 1900)]
            await interaction.followup.send(f"**{interaction.user.display_name} perguntou:** {pergunta}\n\n{chunks[0]}")
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)
    except Exception as e:
        print(f"[ERRO] comando /ai falhou: {e}")
        await interaction.followup.send("❌ A IA está indisponível no momento. Tente novamente!")


# ─── Comando /ban ─────────────────────────────────────────────────────────────

@bot.tree.command(name="ban", description="Bane um membro do servidor")
@app_commands.describe(membro="O membro a ser banido", motivo="Motivo do banimento", dias="Dias de mensagens a deletar (0-7)")
@app_commands.default_permissions(ban_members=True)
async def ban_command(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Sem motivo informado", dias: app_commands.Range[int, 0, 7] = 0):
    await interaction.response.defer(ephemeral=True)

    if not membro.is_bannable():
        await interaction.followup.send("❌ Não consigo banir esse membro (cargo superior ou igual ao meu).")
        return

    try:
        await membro.ban(delete_message_days=dias, reason=motivo)
        embed = discord.Embed(title="🔨 Membro Banido", color=0xFF0000)
        embed.add_field(name="Membro", value=str(membro), inline=True)
        embed.add_field(name="Motivo", value=motivo, inline=True)
        embed.add_field(name="Moderador", value=str(interaction.user), inline=True)
        await interaction.followup.send(embed=embed)
        try:
            await membro.send(f"Você foi **banido** do servidor **{interaction.guild.name}**.\nMotivo: {motivo}")
        except Exception:
            pass
    except Exception as e:
        print(f"[ERRO] ao banir: {e}")
        await interaction.followup.send("❌ Erro ao banir o membro.")


# ─── Comando /kick ────────────────────────────────────────────────────────────

@bot.tree.command(name="kick", description="Expulsa um membro do servidor")
@app_commands.describe(membro="O membro a ser expulso", motivo="Motivo da expulsão")
@app_commands.default_permissions(kick_members=True)
async def kick_command(interaction: discord.Interaction, membro: discord.Member, motivo: str = "Sem motivo informado"):
    await interaction.response.defer(ephemeral=True)

    if not membro.is_kickable():
        await interaction.followup.send("❌ Não consigo expulsar esse membro (cargo superior ou igual ao meu).")
        return

    try:
        await membro.kick(reason=motivo)
        embed = discord.Embed(title="👟 Membro Expulso", color=0xFF8800)
        embed.add_field(name="Membro", value=str(membro), inline=True)
        embed.add_field(name="Motivo", value=motivo, inline=True)
        embed.add_field(name="Moderador", value=str(interaction.user), inline=True)
        await interaction.followup.send(embed=embed)
        try:
            await membro.send(f"Você foi **expulso** do servidor **{interaction.guild.name}**.\nMotivo: {motivo}")
        except Exception:
            pass
    except Exception as e:
        print(f"[ERRO] ao expulsar: {e}")
        await interaction.followup.send("❌ Erro ao expulsar o membro.")


# ─── Comando /timeout ─────────────────────────────────────────────────────────

@bot.tree.command(name="timeout", description="Silencia um membro temporariamente")
@app_commands.describe(membro="O membro a ser silenciado", minutos="Duração em minutos (1–40320)", motivo="Motivo")
@app_commands.default_permissions(moderate_members=True)
async def timeout_command(interaction: discord.Interaction, membro: discord.Member, minutos: app_commands.Range[int, 1, 40320] = 10, motivo: str = "Sem motivo informado"):
    await interaction.response.defer(ephemeral=True)

    try:
        await membro.timeout(timedelta(minutes=minutos), reason=motivo)
        duracao = f"{minutos // 60}h {minutos % 60}min" if minutos >= 60 else f"{minutos} minutos"
        embed = discord.Embed(title="🔇 Membro Silenciado", color=0xFFCC00)
        embed.add_field(name="Membro", value=str(membro), inline=True)
        embed.add_field(name="Duração", value=duracao, inline=True)
        embed.add_field(name="Motivo", value=motivo, inline=False)
        embed.add_field(name="Moderador", value=str(interaction.user), inline=True)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"[ERRO] ao aplicar timeout: {e}")
        await interaction.followup.send("❌ Erro ao silenciar o membro.")


# ─── Advertências ─────────────────────────────────────────────────────────────

warn_store: dict[int, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))


@bot.tree.command(name="warn", description="Adverte um membro do servidor")
@app_commands.describe(membro="O membro a ser advertido", motivo="Motivo da advertência")
@app_commands.default_permissions(moderate_members=True)
async def warn_command(interaction: discord.Interaction, membro: discord.Member, motivo: str):
    await interaction.response.defer(ephemeral=True)

    from datetime import datetime
    warn_store[interaction.guild_id][membro.id].append({
        "motivo": motivo,
        "data": datetime.now().strftime("%d/%m/%Y"),
        "moderador": str(interaction.user)
    })
    total = len(warn_store[interaction.guild_id][membro.id])

    embed = discord.Embed(title="⚠️ Advertência Aplicada", color=0xFFA500)
    embed.add_field(name="Membro", value=str(membro), inline=True)
    embed.add_field(name="Total de Avisos", value=str(total), inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.add_field(name="Moderador", value=str(interaction.user), inline=True)
    await interaction.followup.send(embed=embed)

    try:
        await membro.send(
            f"Você recebeu uma **advertência** no servidor **{interaction.guild.name}**.\n"
            f"Motivo: {motivo}\nTotal de advertências: {total}"
        )
    except Exception:
        pass


@bot.tree.command(name="warns", description="Mostra as advertências de um membro")
@app_commands.describe(membro="O membro a consultar")
@app_commands.default_permissions(moderate_members=True)
async def warns_command(interaction: discord.Interaction, membro: discord.Member):
    await interaction.response.defer(ephemeral=True)

    avisos = warn_store[interaction.guild_id][membro.id]
    if not avisos:
        await interaction.followup.send(f"✅ **{membro}** não possui advertências.")
        return

    desc = "\n\n".join(
        f"**{i+1}.** {w['motivo']}\n> Por {w['moderador']} em {w['data']}"
        for i, w in enumerate(avisos)
    )
    embed = discord.Embed(title=f"⚠️ Advertências de {membro}", description=desc, color=0xFFA500)
    embed.set_footer(text=f"Total: {len(avisos)} advertência(s)")
    await interaction.followup.send(embed=embed)


# ─── Comando /clear ───────────────────────────────────────────────────────────

@bot.tree.command(name="clear", description="Apaga mensagens do canal")
@app_commands.describe(quantidade="Número de mensagens a apagar (1–100)", membro="Apagar somente mensagens deste membro")
@app_commands.default_permissions(manage_messages=True)
async def clear_command(interaction: discord.Interaction, quantidade: app_commands.Range[int, 1, 100] = 10, membro: discord.Member = None):
    await interaction.response.defer(ephemeral=True)

    def check(msg):
        return membro is None or msg.author == membro

    try:
        deleted = await interaction.channel.purge(limit=quantidade if not membro else 200, check=check)
        deleted = deleted[:quantidade]
        await interaction.followup.send(
            f"✅ {len(deleted)} mensagem(s) apagada(s)" + (f" de **{membro}**" if membro else "") + "."
        )
    except Exception as e:
        print(f"[ERRO] ao apagar mensagens: {e}")
        await interaction.followup.send("❌ Erro ao apagar mensagens.")


# ─── Comando /userinfo ────────────────────────────────────────────────────────

@bot.tree.command(name="userinfo", description="Mostra informações sobre um membro")
@app_commands.describe(membro="O membro a consultar (padrão: você mesmo)")
async def userinfo_command(interaction: discord.Interaction, membro: discord.Member = None):
    await interaction.response.defer()

    alvo = membro or interaction.user
    avisos = warn_store[interaction.guild_id][alvo.id]

    embed = discord.Embed(title=f"👤 {alvo}", color=0x5865F2)
    embed.set_thumbnail(url=alvo.display_avatar.url)
    embed.add_field(name="ID", value=str(alvo.id), inline=True)
    embed.add_field(name="Conta criada em", value=f"<t:{int(alvo.created_at.timestamp())}:D>", inline=True)

    if isinstance(alvo, discord.Member):
        if alvo.joined_at:
            embed.add_field(name="Entrou no servidor", value=f"<t:{int(alvo.joined_at.timestamp())}:D>", inline=True)
        cargo_top = alvo.top_role.name if alvo.top_role.name != "@everyone" else "Nenhum"
        embed.add_field(name="Cargo mais alto", value=cargo_top, inline=True)
        if alvo.nick:
            embed.add_field(name="Apelido", value=alvo.nick, inline=True)

    embed.add_field(
        name="⚠️ Advertências",
        value="Nenhuma" if not avisos else f"{len(avisos)} advertência(s)",
        inline=True
    )
    embed.timestamp = discord.utils.utcnow()
    await interaction.followup.send(embed=embed)


# ─── Iniciar ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        print("[ERRO] DISCORD_BOT_TOKEN não definido!")
        exit(1)
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"[ERRO] Falha ao iniciar o bot: {e}")
