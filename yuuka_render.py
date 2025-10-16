import discord
from discord.ext import commands
from discord import app_commands
import google.generativeai as genai
import os
from dotenv import load_dotenv
import asyncio
import io
from PIL import Image
import glob
import json
import websockets
from typing import Union
from collections import defaultdict

# .env 파일이 저장될 영구 디스크 경로 (Render 환경 변수에서 가져옴)
# 로컬 테스트를 위해 기본값으로 '.env'를 사용합니다.
ENV_FILE_PATH = os.getenv("ENV_FILE_PATH", ".env")

# .env 파일 불러오기 (초기 로드용)
load_dotenv(dotenv_path=ENV_FILE_PATH)

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # 기본 활동 채널
# ❗️ 새 환경 변수: 웹페이지의 기본 URL을 설정해야 합니다.
# 예: "https://your-app-name.onrender.com"
WEB_BASE_URL = os.getenv("WEB_BASE_URL")


if not all([DISCORD_BOT_TOKEN, GEMINI_API_KEY, WEB_BASE_URL]):
    print("오류: 필수 환경 변수가 설정되지 않았습니다.")
    print("필요한 변수: DISCORD_BOT_TOKEN, GEMINI_API_KEY, WEB_BASE_URL")
    # 로컬이 아닌 환경에서는 종료 처리
    if not os.path.exists(".env"):
        exit()

def update_env_variable(key: str, value: str):
    """
    지정된 경로의 .env 파일(.env)의 특정 변수를 업데이트하는 함수
    """
    lines = []
    found = False
    
    env_dir = os.path.dirname(ENV_FILE_PATH)
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        
    if os.path.exists(ENV_FILE_PATH):
        with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
        for line in lines:
            if line.strip().startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"{key}={value}\n")

    os.environ[key] = value
    print(f"'{ENV_FILE_PATH}' 파일에 {key}={value} 로 업데이트 완료 및 환경 변수 적용")

def load_persona_prompt(file_path="persona.txt"):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"오류: 페르소나 파일 '{file_path}'을 찾을 수 없습니다.")
        exit()

GEM_PROMPT = load_persona_prompt()
knowledge_cache = {}
KNOWLEDGE_BASE_DIR = "knowledge_base"
processing_lock = asyncio.Lock()
chat_sessions = {}

# 서버 ID(guild_id)를 키로 사용하여 웹소켓 클라이언트 집합을 관리
clients_by_guild = defaultdict(set)
# 웹소켓 객체를 키로 사용하여 서버 ID를 추적 (연결 종료 시 정리용)
client_to_guild = {}

# 감정 키워드 매핑
EMOTION_SPRITE_MAP = {
    "neutral": "yuuka_neutral.png",
    "neutral2": "yuuka_neutral2.png",
    "neutral3": "yuuka_neutral3.png",
    "smile": "yuuka_smile.png",
    "smile2": "yuuka_smile2.png",
    "blush": "yuuka_blush.png",
    "angry": "yuuka_angry.png",
    "angry2": "yuuka_angry2.png",
}

# 웹소켓 관련 함수 (서버 ID 기반으로 변경)
async def broadcast_to_clients(guild_id: str, message_data: dict):
    """지정된 서버 ID에 연결된 모든 클라이언트에게 메시지를 전송합니다."""
    if guild_id in clients_by_guild:
        clients = clients_by_guild[guild_id]
        if clients:
            message_json = json.dumps(message_data)
            # asyncio.gather를 사용하여 동시에 여러 클라이언트에게 전송
            await asyncio.gather(*[client.send(message_json) for client in clients])
            print(f"서버 ID '{guild_id}'의 {len(clients)}개 클라이언트로 데이터 전송: {message_json}")

async def websocket_handler(websocket):
    """웹 클라이언트 연결을 처리하고, 서버 ID에 따라 등록 및 관리합니다."""
    print(f"웹 클라이언트 연결 시도: {websocket.remote_address}")
    try:
        # 클라이언트로부터 첫 메시지(등록 정보)를 기다림
        message = await websocket.recv()
        data = json.loads(message)

        # 등록 메시지인지, guild_id가 포함되어 있는지 확인
        if data.get("type") == "register" and "guild_id" in data:
            guild_id = data["guild_id"]
            clients_by_guild[guild_id].add(websocket)
            client_to_guild[websocket] = guild_id
            print(f"클라이언트 {websocket.remote_address}가 서버 ID '{guild_id}'에 등록되었습니다.")
            
            # 연결이 끊길 때까지 대기
            await websocket.wait_closed()
        else:
            # 첫 메시지가 유효한 등록 정보가 아니면 연결 종료
            print(f"잘못된 등록 메시지 수신. 연결을 닫습니다: {message}")
            await websocket.close()

    except (websockets.exceptions.ConnectionClosedError, json.JSONDecodeError, KeyError) as e:
        print(f"클라이언트 등록 중 오류 또는 연결 종료: {e}")
    finally:
        # 클라이언트 연결 종료 시, 관리 목록에서 제거
        if websocket in client_to_guild:
            guild_id = client_to_guild[websocket]
            clients_by_guild[guild_id].remove(websocket)
            # 해당 서버에 더 이상 연결된 클라이언트가 없으면 키 삭제
            if not clients_by_guild[guild_id]:
                del clients_by_guild[guild_id]
            del client_to_guild[websocket]
            print(f"서버 ID '{guild_id}'에서 클라이언트 연결 해제됨: {websocket.remote_address}")
        else:
            print(f"등록되지 않은 클라이언트 연결 해제됨: {websocket.remote_address}")

# Knowledge Base 로딩
def load_knowledge_base():
    global knowledge_cache
    knowledge_cache = {}
    
    file_patterns = [
        f"{KNOWLEDGE_BASE_DIR}/*.txt", f"{KNOWLEDGE_BASE_DIR}/*.md",
        f"{KNOWLEDGE_BASE_DIR}/*.png", f"{KNOWLEDGE_BASE_DIR}/*.jpg",
        f"{KNOWLEDGE_BASE_DIR}/*.jpeg", f"{KNOWLEDGE_BASE_DIR}/*.webp"
    ]
    files_to_load = []
    for pattern in file_patterns:
        files_to_load.extend(glob.glob(pattern))
    
    if not os.path.exists(KNOWLEDGE_BASE_DIR):
        print(f"경고: '{KNOWLEDGE_BASE_DIR}' 폴더를 찾을 수 없습니다.")
        return

    print(f"'{KNOWLEDGE_BASE_DIR}' 폴더에서 지식 베이스 파일을 로드합니다...")
    for file_path in files_to_load:
        file_name = os.path.basename(file_path)
        try:
            if file_path.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                knowledge_cache[file_name] = Image.open(file_path)
                print(f" - 성공 (이미지): '{file_name}'")
            else:
                with open(file_path, 'r', encoding='utf-8') as f:
                    knowledge_cache[file_name] = f.read()
                    print(f" - 성공 (텍스트): '{file_name}'")
        except Exception as e:
            print(f" - 실패: '{file_path}' 파일을 읽는 중 오류 발생: {e}")
    print("지식 베이스 로딩 완료!")

# Gemini 설정
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash', system_instruction=GEM_PROMPT)

# Discord 봇 기본 설정
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'로그인되었습니다! 봇 이름: {bot.user}')
    load_knowledge_base()
    try:
        await bot.tree.sync()
        print(f"전역 슬래시 명령어 동기화 완료.")
    except Exception as e:
        print(f"명령어 동기화 실패: {e}")
    
    global CHANNEL_ID
    CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
    print(f'-----------------------------------------\n봇이 채널 ID {CHANNEL_ID}에서 메시지를 기다리고 있습니다.')


# ✅ /채널지정 명령어
@bot.tree.command(name="채널지정", description="유우카가 활동할 채널을 지정합니다.")
async def set_channel(interaction: discord.Interaction, channel: Union[discord.TextChannel, discord.VoiceChannel]):
    global CHANNEL_ID
    CHANNEL_ID = channel.id
    update_env_variable("CHANNEL_ID", str(CHANNEL_ID))
    await interaction.response.send_message(
        f"유우카의 활동 채널이 <#{CHANNEL_ID}> 로 설정되었어요! 이제부터 이 채널에서만 대화할게요.",
        ephemeral=False
    )

@set_channel.error
async def set_channel_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("이 명령어는 서버 관리자만 사용할 수 있어요!", ephemeral=True)
    else:
        await interaction.response.send_message(f"오류가 발생했어요: {error}", ephemeral=True)

# ✅ /새대화 명령어
@bot.tree.command(name="새대화", description="유우카와의 대화를 초기화합니다.")
async def reset_conversation(interaction: discord.Interaction):
    current_channel_id = int(os.getenv("CHANNEL_ID", "0"))
    if interaction.channel.id != current_channel_id:
        await interaction.response.send_message(
            f"이 명령어는 지정된 채널(<#{current_channel_id}>)에서만 사용할 수 있어요.",
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    chat_sessions[guild_id] = model.start_chat(history=[])
    print(f"관리자({interaction.user})가 대화를 초기화했습니다. (서버: {interaction.guild.name})")
    await interaction.response.send_message(f"{interaction.user.mention} 알겠습니다! 새로운 대화를 시작할게요 ✨")

# ✅ /지식갱신 명령어
@bot.tree.command(name="지식갱신", description="Knowledge Base 폴더의 파일들을 다시 불러옵니다.")
async def reload_knowledge(interaction: discord.Interaction):
    current_channel_id = int(os.getenv("CHANNEL_ID", "0"))
    if interaction.channel.id != current_channel_id:
        await interaction.response.send_message(
            f"이 명령어는 지정된 채널(<#{current_channel_id}>)에서만 사용할 수 있어요.",
            ephemeral=True
        )
        return

    await interaction.response.defer()
    print(f"{interaction.user}님이 지식 베이스를 새로고침했습니다. (서버: {interaction.guild.name})")
    load_knowledge_base()
    await interaction.followup.send(f"지식 파일들을 새로고침했어요! ({len(knowledge_cache)}개 파일 로드됨)")

# ✅ /주소 명령어
@bot.tree.command(name="주소", description="이 서버의 대화를 볼 수 있는 웹페이지 주소를 안내합니다.")
async def get_address(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    # index.html 파일이 기본 경로에 있다고 가정
    page_url = f"{WEB_BASE_URL}/index.html?guild_id={guild_id}"
    
    embed = discord.Embed(
        title="샬레 집무실 주소",
        description=f"안녕하세요, 선생님! 이 서버의 대화는 아래의 전용 주소에서 실시간으로 확인할 수 있어요.",
        color=0x5dadec # 블루 아카이브 테마 색상
    )
    embed.add_field(name="URL", value=f"[여기를 클릭하여 집무실로 이동]({page_url})", inline=False)
    embed.set_footer(text="이 주소는 현재 서버에만 해당돼요!")
    
    # ephemeral=True : 명령어를 실행한 사용자에게만 보이게 설정
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ❗️ 최종 수정된 on_message 함수
@bot.event
async def on_message(message):
    if message.author == bot.user or not message.guild:
        return

    current_channel_id = int(os.getenv("CHANNEL_ID", "0"))
    if message.channel.id != current_channel_id:
        return
    
    guild_id = message.guild.id
    guild_id_str = str(guild_id)

    if guild_id_str not in clients_by_guild:
        print(f"서버 ID '{guild_id_str}'에 연결된 웹 클라이언트 없음 — 메시지 처리 중단.")
        return

    async with processing_lock:
        if guild_id not in chat_sessions:
            chat_sessions[guild_id] = model.start_chat(history=[])

        current_session = chat_sessions[guild_id]
        user_nickname = message.author.display_name
        user_message = message.content.strip()
        
        if not user_message and not message.attachments:
            return
        
        prompt_parts = []
        knowledge_text_context = ""
        if knowledge_cache:
            knowledge_text_context += "--- 참고 자료 ---\n"
            for file_name, content in knowledge_cache.items():
                if isinstance(content, str):
                    knowledge_text_context += f"\n[파일: {file_name}]\n{content}\n"
                elif isinstance(content, Image.Image):
                    prompt_parts.append(content)
            knowledge_text_context += "--- 끝 ---\n\n"

        full_text_prompt = f"{knowledge_text_context}내 이름은 '{user_nickname}'이야.\n\n{user_message}"
        prompt_parts.insert(0, full_text_prompt)

        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith('image/'):
                try:
                    image_bytes = await attachment.read()
                    img = Image.open(io.BytesIO(image_bytes))
                    prompt_parts.append(img)
                    print(f"사용자 첨부 이미지 추가: {attachment.filename}")
                except Exception as e:
                    print(f"첨부 이미지 처리 중 오류 발생: {e}")

        async with message.channel.typing():
            try:
                response = await asyncio.to_thread(current_session.send_message, prompt_parts)
                raw_response = response.text
                print(f"Gemini 원본 응답: {raw_response}")

                dialogue_text = ""
                sprite_filename = EMOTION_SPRITE_MAP["neutral"] # 기본값 설정

                try:
                    # 1. 응답에서 JSON 문자열 부분만 정확히 추출
                    json_str = ""
                    if "```json" in raw_response:
                        json_str = raw_response.split("```json")[1].split("```")[0].strip()
                    else:
                        json_start = raw_response.find('{')
                        json_end = raw_response.rfind('}') + 1
                        if json_start != -1 and json_end > json_start:
                            json_str = raw_response[json_start:json_end]

                    # 2. 추출된 문자열이 있다면 JSON으로 파싱
                    if json_str:
                        gemini_data = json.loads(json_str)
                        dialogue_text = gemini_data.get("text", "...")
                        emotion_key = gemini_data.get("emotion", "neutral")
                        sprite_filename = EMOTION_SPRITE_MAP.get(emotion_key, EMOTION_SPRITE_MAP["neutral"])
                    else:
                        # 3. JSON 구조를 찾지 못하면, 원본 응답을 일반 텍스트로 사용
                        print("JSON 구조를 찾지 못했습니다. 일반 텍스트로 처리합니다.")
                        dialogue_text = raw_response.strip()

                except (json.JSONDecodeError, KeyError) as e:
                    # 4. JSON 파싱에 실패하면, 원본 응답을 정리하여 일반 텍스트로 사용
                    print(f"JSON 파싱 실패 ({e}). 응답을 일반 텍스트로 처리합니다.")
                    dialogue_text = raw_response.replace("```json", "").replace("```", "").strip()
                
                # 최종적으로 정리된 데이터를 클라이언트로 전송
                await broadcast_to_clients(guild_id_str, {"text": dialogue_text, "sprite": sprite_filename})

            except Exception as e:
                print(f"Gemini API 호출 중 심각한 오류 발생: {e}")
                await message.channel.send(f"으앗, 선생님 죄송해요. 생각에 잠시 오류가 생긴 것 같아요: `{e}`")

# 실행
async def main():
    port = int(os.environ.get("PORT", 8765))
    
    async with websockets.serve(websocket_handler, "0.0.0.0", port):
        print(f"웹소켓 서버가 ws://0.0.0.0:{port} 에서 시작되었습니다.")
        await bot.start(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:

        print("\n봇을 종료합니다.")
