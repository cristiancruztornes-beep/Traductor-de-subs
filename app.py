import hashlib, io, os, re, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import srt
import telebot
from flask import Flask, request
from deep_translator import GoogleTranslator
from charset_normalizer import from_bytes

# ---------- Configuración (variables de entorno en Render) ----------
TOKEN = os.environ['TELEGRAM_TOKEN']
URL = os.environ.get('RENDER_EXTERNAL_URL')   # Render la define sola
ALLOWED_IDS = {int(x) for x in os.environ.get('ALLOWED_IDS', '').split(',') if x.strip()}  # vacío = cualquiera
HILOS = 8      # traducciones en paralelo
MAX_MB = 20    # límite de Telegram para descargar archivos
SECRETO = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
vistos = deque(maxlen=500)

TAG_ASS = re.compile(r'\{\\[^}]*\}')
TAG_HTML = re.compile(r'</?[^>]+>')
TIENE_LETRAS = re.compile(r'[^\W\d_]')


def decodificar(datos):
    mejor = from_bytes(datos).best()
    texto = str(mejor) if mejor is not None else datos.decode('utf-8', errors='replace')
    return texto.lstrip('\ufeff')


def preparar(texto):
    prefijo = ''.join(TAG_ASS.findall(texto))
    sin_ass = TAG_ASS.sub('', texto)
    cursiva = bool(re.match(r'\s*<i>', sin_ass))
    limpio = TAG_HTML.sub('', sin_ass).strip()
    return prefijo, cursiva, limpio


def traducir_texto(texto, intentos=4):
    for i in range(intentos):
        try:
            r = GoogleTranslator(source='auto', target='es').translate(texto)
            if r:
                return r
        except Exception:
            time.sleep(1 + i)
    return texto   # si falla, deja la línea original


def traducir_subs(subs, avance):
    datos = [preparar(s.content) for s in subs]
    unicos = sorted({d[2] for d in datos if TIENE_LETRAS.search(d[2])})
    cache, total = {}, len(unicos)
    with ThreadPoolExecutor(HILOS) as ex:
        for i, (orig, trad) in enumerate(zip(unicos, ex.map(traducir_texto, unicos)), 1):
            cache[orig] = trad
            avance(i, total)
    for s, (prefijo, cursiva, limpio) in zip(subs, datos):
        t = cache.get(limpio, limpio)
        if cursiva:
            t = f'<i>{t}</i>'
        s.content = prefijo + t
    return subs


def crear_avance(chat_id, msg_id):
    ultimo = [0.0]
    def avance(i, total):
        ahora = time.time()
        if i == total or ahora - ultimo[0] >= 4:
            ultimo[0] = ahora
            try:
                bot.edit_message_text(f'Traduciendo… {i * 100 // max(total, 1)}%', chat_id, msg_id)
            except Exception:
                pass
    return avance


@bot.message_handler(commands=['start', 'help'])
def inicio(m):
    bot.reply_to(m, 'Mándame un archivo .srt y te lo devuelvo traducido al español. '
                    'Detecto el idioma original automáticamente.')


@bot.message_handler(content_types=['document'])
def recibir(m):
    if ALLOWED_IDS and m.from_user.id not in ALLOWED_IDS:
        return
    doc = m.document
    nombre = doc.file_name or 'subtitulo.srt'
    if not nombre.lower().endswith('.srt'):
        bot.reply_to(m, 'Solo acepto archivos .srt')
        return
    if doc.file_size and doc.file_size > MAX_MB * 1024 * 1024:
        bot.reply_to(m, f'El archivo pasa de {MAX_MB} MB.')
        return
    estado = bot.reply_to(m, 'Descargando…')
    try:
        info = bot.get_file(doc.file_id)
        datos = bot.download_file(info.file_path)
        subs = list(srt.parse(decodificar(datos), ignore_errors=True))
        if not subs:
            bot.edit_message_text('No encontré subtítulos válidos en ese archivo.',
                                  m.chat.id, estado.message_id)
            return
        subs = traducir_subs(subs, crear_avance(m.chat.id, estado.message_id))
        salida = io.BytesIO(srt.compose(subs).encode('utf-8'))
        salida.name = nombre[:-4] + '.es.srt'
        bot.send_document(m.chat.id, salida, caption='Listo ✅')
        bot.delete_message(m.chat.id, estado.message_id)
    except Exception as e:
        try:
            bot.edit_message_text(f'Error: {e}', m.chat.id, estado.message_id)
        except Exception:
            pass


# ---------- Webhook ----------
@app.route('/')
def salud():
    return 'ok'


@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != SECRETO:
        return 'forbidden', 403
    upd = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
    if upd.update_id not in vistos:          # evita procesar dos veces un reintento
        vistos.append(upd.update_id)
        threading.Thread(target=bot.process_new_updates, args=([upd],), daemon=True).start()
    return 'ok'                              # responde ya; la traducción sigue en segundo plano


if URL:
    try:
        bot.set_webhook(url=f'{URL}/webhook', secret_token=SECRETO)
    except Exception as e:
        print('No pude registrar el webhook:', e)
