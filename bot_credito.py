# =========================================
# Bot de WhatsApp: Educación Financiera para el Mundo
# Autora: Jazmín Sandoval
# Descripción: Bot educativo para temas de crédito
# =========================================

from flask import Flask, request, render_template
import json
import os
import re
import unicodedata
import hashlib
import threading
from datetime import datetime, timezone
from collections import deque
from decimal import Decimal, getcontext, localcontext, ROUND_HALF_UP, ROUND_CEILING
from math import log
import requests

# Quita signos de puntuación y espacios sueltos al inicio/final de un mensaje
# (¡Hola!, Hola., ¿menú? etc. deben reconocerse igual que "hola").
_BORDE_PUNTUACION_RE = re.compile(r'^[\s¡!¿?.,;:()"\']+|[\s¡!¿?.,;:()"\']+$')

# Varios celulares mandan los emojis con un "selector de variación" (U+FE0F,
# invisible) o con un modificador de tono de piel (👍🏽, 👍🏿, etc.) pegado al
# emoji base. Esto no cambia su significado pero sí el texto exacto que llega,
# así que lo quitamos para que "👍🏽" o "👍️" sigan reconociéndose como "👍".
_MODIFICADORES_EMOJI_RE = re.compile('[\U0000FE0E\U0000FE0F\U0001F3FB-\U0001F3FF]')

app = Flask(__name__)
getcontext().prec = 17  # Precisión tipo Excel

# Token, ID de número y verify token se leen de variables de entorno
# (configúralas en Render → tu servicio → Environment).
# NUNCA escribas valores reales aquí directamente.
TOKEN = os.environ.get('WHATSAPP_TOKEN')
PHONE_NUMBER_ID = os.environ.get('WHATSAPP_PHONE_NUMBER_ID')
VERIFY_TOKEN = os.environ.get('WHATSAPP_VERIFY_TOKEN')

# Ruta para validar que el sitio está activo (solución para Meta y og:image)
@app.route('/')
def index():
    return render_template('index.html')

# Ruta pública de la política de privacidad (requerida por Meta)
@app.route('/privacidad')
def privacidad():
    return render_template('privacidad.html')

estado_usuario = {}

# Último mensaje que el bot envió a cada número, para poder "explicarlo más
# fácil" si lo piden (ver es_peticion_explicar_mas_facil / _explicar_mas_facil).
_ultimo_mensaje_bot = {}

# =========================================
# Protección contra mensajes duplicados
# =========================================
# WhatsApp reenvía el mismo mensaje si no recibimos su respuesta 200 rápido
# (típico cuando el plan gratuito de Render "despierta" tras estar dormido).
# Sin esto, el bot respondería dos veces al mismo mensaje.
_IDS_PROCESADOS_MAXLEN = 500
_ids_mensajes_procesados = set()
_orden_ids_mensajes_procesados = deque()

def ya_fue_procesado(message_id):
    """
    Devuelve True si ya procesamos este id de mensaje antes (o sea, es un
    reenvío duplicado de WhatsApp) y en ese caso lo ignora. Si es nuevo, lo
    registra y devuelve False para que se procese normalmente.
    Si no viene id (no debería pasar, pero por si acaso), no bloqueamos el
    mensaje: preferimos correr el riesgo de un duplicado a arriesgarnos a
    nunca responder.
    """
    if not message_id:
        return False
    if message_id in _ids_mensajes_procesados:
        return True
    _ids_mensajes_procesados.add(message_id)
    _orden_ids_mensajes_procesados.append(message_id)
    if len(_orden_ids_mensajes_procesados) > _IDS_PROCESADOS_MAXLEN:
        id_viejo = _orden_ids_mensajes_procesados.popleft()
        _ids_mensajes_procesados.discard(id_viejo)
    return False

# =========================================
# Analítica de uso (para investigación sobre el impacto del bot)
# =========================================
# Registramos, de forma anónima, por qué pasos del bot va pasando cada
# persona (nunca el número de teléfono ni el contenido de sus mensajes).
# Se guarda en una tabla de Supabase (Postgres) si está configurada (ver
# variables de entorno abajo), usando su API REST directamente con
# "requests" (ya es una dependencia del bot, no hace falta instalar nada
# más). Si no está configurada, o si algo falla, simplemente no se registra
# nada y la conversación sigue normal: esto NUNCA debe romper la
# experiencia de quien está usando el bot.
ID_ANONIMO_SALT = os.environ.get('ID_ANONIMO_SALT', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
SUPABASE_TABLA_EVENTOS = os.environ.get('SUPABASE_TABLA_EVENTOS', 'eventos')
SUPABASE_TABLA_SESIONES = os.environ.get('SUPABASE_TABLA_SESIONES', 'sesiones_activas')

def _id_anonimo(numero):
    """
    Convierte un número de teléfono en un identificador anónimo estable (el
    mismo número siempre produce el mismo id, así se pueden ver los pasos de
    una misma persona sin saber quién es). Usa una "sal" secreta para que no
    se pueda recuperar el número original probando todos los números
    telefónicos posibles.
    """
    texto = (ID_ANONIMO_SALT + numero).encode('utf-8')
    return hashlib.sha256(texto).hexdigest()[:16]

# Una "sesión" agrupa los eventos de una misma visita: si pasan más de 30
# minutos sin actividad de una persona, el siguiente evento empieza una
# sesión nueva (es el mismo criterio que usan la mayoría de las
# herramientas de analítica web). Esto se guarda solo en memoria, así que
# si el servicio se reinicia (ver mensaje_sesion_reiniciada) una sesión en
# curso se puede cortar antes de tiempo; es una limitación aceptable.
_VENTANA_SESION_MINUTOS = 30
_ultima_actividad_por_numero = {}
_sesion_actual_por_numero = {}

def _id_sesion_y_duracion(numero):
    """
    Devuelve (id_sesion, segundos_desde_paso_anterior) para el evento que se
    está por registrar. El id de sesión cambia cuando no había actividad
    previa de esta persona o cuando pasó más de _VENTANA_SESION_MINUTOS
    desde su último evento. segundos_desde_paso_anterior es None cuando es
    el primer evento de una sesión (no hay "paso anterior" con el cual
    comparar dentro de esa misma visita).
    """
    ahora = datetime.now(timezone.utc)
    anterior = _ultima_actividad_por_numero.get(numero)
    if anterior is not None:
        segundos = (ahora - anterior).total_seconds()
    else:
        segundos = None
    if anterior is None or segundos > _VENTANA_SESION_MINUTOS * 60:
        texto = f"{ID_ANONIMO_SALT}{numero}{ahora.isoformat()}".encode('utf-8')
        _sesion_actual_por_numero[numero] = hashlib.sha256(texto).hexdigest()[:12]
        segundos = None
    _ultima_actividad_por_numero[numero] = ahora
    return _sesion_actual_por_numero[numero], segundos

def _analitica_disponible():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def _registrar_evento_uso(numero, estado_antes, estado_despues):
    """
    Guarda una fila con el paso anterior y el paso nuevo por el que pasó una
    persona (identificada solo por su id anónimo), a qué sesión (visita)
    pertenece, y cuántos segundos pasaron desde su evento anterior dentro de
    esa misma sesión. Se ejecuta en un hilo aparte para no hacer más lenta
    la respuesta al usuario, y cualquier error se ignora silenciosamente:
    esto nunca debe interrumpir la conversación de alguien.
    """
    # El id de sesión y la duración se calculan aquí, no dentro del hilo:
    # así, si llegan dos eventos casi al mismo tiempo para el mismo número,
    # no hay riesgo de que se les asigne una sesión distinta por una
    # condición de carrera entre hilos.
    id_sesion, segundos_desde_anterior = _id_sesion_y_duracion(numero)

    def _tarea():
        if not _analitica_disponible():
            return
        try:
            fila = {
                "fecha_hora_utc": datetime.now(timezone.utc).isoformat(),
                "id_anonimo": _id_anonimo(numero),
                "id_sesion": id_sesion,
                "estado_antes": str(estado_antes),
                "estado_despues": str(estado_despues),
                "segundos_desde_paso_anterior": segundos_desde_anterior,
            }
            url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLA_EVENTOS}"
            headers = {
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            respuesta = requests.post(url, headers=headers, json=fila, timeout=10)
            if respuesta.status_code >= 300:
                print(f"⚠️ Supabase rechazó el evento de uso ({respuesta.status_code}): {respuesta.text}")
        except Exception as e:
            print("⚠️ No se pudo registrar el evento de uso:", e)
    threading.Thread(target=_tarea, daemon=True).start()

# =========================================
# Persistencia de la conversación en curso
# =========================================
# estado_usuario vive en memoria, así que se perdía por completo cada vez
# que Render reiniciaba el servicio (por ejemplo, tras dormirse por
# inactividad en el plan gratuito): quien estuviera a la mitad de algo caía
# en mensaje_sesion_reiniciada y tenía que empezar de nuevo. Para evitarlo,
# cada vez que el estado de una persona cambia se guarda también en
# Supabase (misma tabla de credenciales que la analítica de uso, usando el
# mismo id anónimo como llave, nunca el número real), y si el bot arranca
# de cero y alguien escribe, primero se intenta recuperar su estado antes
# de asumir que es una conversación nueva. Igual que la analítica, esto es
# opcional (si Supabase no está configurado, o algo falla, simplemente no
# hay persistencia y el bot sigue funcionando como antes) y nunca debe
# interrumpir la conversación de alguien.
_SESION_EXPIRA_HORAS = 6

def _json_default(obj):
    if isinstance(obj, Decimal):
        return {"__decimal__": str(obj)}
    raise TypeError(f"Objeto no serializable en el estado de la conversación: {type(obj)}")

def _json_object_hook(d):
    if len(d) == 1 and "__decimal__" in d:
        return Decimal(d["__decimal__"])
    return d

def _guardar_estado_sesion(numero):
    """
    Guarda (en un hilo aparte, sin bloquear la respuesta) el estado actual
    de la conversación de esta persona, para poder recuperarlo si el
    servicio se reinicia antes de que termine su flujo.
    """
    estado_actual = estado_usuario.get(numero, {})

    def _tarea():
        if not _analitica_disponible():
            return
        try:
            fila = {
                "id_anonimo": _id_anonimo(numero),
                "estado": json.dumps(estado_actual, default=_json_default),
                "actualizado_en": datetime.now(timezone.utc).isoformat(),
            }
            url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLA_SESIONES}?on_conflict=id_anonimo"
            headers = {
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            }
            respuesta = requests.post(url, headers=headers, json=fila, timeout=10)
            if respuesta.status_code >= 300:
                print(f"⚠️ Supabase rechazó guardar la sesión ({respuesta.status_code}): {respuesta.text}")
        except Exception as e:
            print("⚠️ No se pudo guardar la sesión:", e)
    threading.Thread(target=_tarea, daemon=True).start()

def _cargar_estado_sesion(numero):
    """
    Intenta recuperar de Supabase el estado guardado de esta persona. Es la
    única parte de la persistencia que es síncrona (bloquea la respuesta),
    porque hace falta el resultado antes de seguir procesando el mensaje;
    por eso usa un timeout corto y solo se llama cuando numero no está ya
    en memoria (o sea, como mucho una vez por persona por cada reinicio del
    servicio, no en cada mensaje). Si algo falla, o la sesión guardada ya es
    muy vieja, devuelve None y el bot sigue como si no hubiera nada guardado.
    """
    if not _analitica_disponible():
        return None
    try:
        url = (
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLA_SESIONES}"
            f"?id_anonimo=eq.{_id_anonimo(numero)}&select=estado,actualizado_en"
        )
        headers = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        }
        respuesta = requests.get(url, headers=headers, timeout=4)
        if respuesta.status_code >= 300:
            print(f"⚠️ Supabase rechazó leer la sesión ({respuesta.status_code}): {respuesta.text}")
            return None
        filas = respuesta.json()
        if not filas:
            return None
        fila = filas[0]
        actualizado_en = datetime.fromisoformat(fila["actualizado_en"].replace("Z", "+00:00"))
        antiguedad_horas = (datetime.now(timezone.utc) - actualizado_en).total_seconds() / 3600
        if antiguedad_horas > _SESION_EXPIRA_HORAS:
            return None
        return json.loads(fila["estado"], object_hook=_json_object_hook)
    except Exception as e:
        print("⚠️ No se pudo recuperar la sesión guardada:", e)
        return None

# =========================================
# Cálculo de pago fijo (tipo Excel)
# =========================================
def calcular_pago_fijo_excel(monto, tasa, plazo):
    P = Decimal(str(monto))
    r = Decimal(str(tasa))
    n = Decimal(str(plazo))
    uno_mas_r = Decimal('1') + r
    base_elevada = uno_mas_r ** n
    inverso = Decimal('1') / base_elevada
    denominador = Decimal('1') - inverso
    numerador = P * r
    pago = numerador / denominador
    return pago.quantize(Decimal('0.01'))

# =========================================
# Frecuencias de pago: para que la persona responda con la tasa ANUAL
# que le ofrecieron (como normalmente se la dicen) en vez de tener que
# convertirla ella misma al periodo de pago.
# =========================================
FRECUENCIAS_PAGO = {
    "1": ("mensual", Decimal("12")),
    "2": ("quincenal", Decimal("24")),
    "3": ("catorcenal", Decimal("26")),
    "4": ("semanal", Decimal("52")),
}

MENSAJE_FRECUENCIA = (
    "¿Con qué frecuencia vas a pagar?\n"
    "1️⃣ Mensual\n"
    "2️⃣ Quincenal (cada 15 días)\n"
    "3️⃣ Catorcenal (cada 14 días)\n"
    "4️⃣ Semanal\n"
    "5️⃣ Otra frecuencia (tú me dices cuántos pagos haces al año)"
)

MENSAJE_FRECUENCIA_AHORRO = (
    "¿Con qué frecuencia vas a apartar dinero?\n"
    "1️⃣ Mensual\n"
    "2️⃣ Quincenal (cada 15 días)\n"
    "3️⃣ Catorcenal (cada 14 días)\n"
    "4️⃣ Semanal\n"
    "5️⃣ Otra frecuencia (tú me dices cuántas veces al año)"
)

MENSAJE_FRECUENCIA_INVERSION = (
    "¿Con qué frecuencia vas a aportar a tu inversión?\n"
    "1️⃣ Mensual\n"
    "2️⃣ Quincenal (cada 15 días)\n"
    "3️⃣ Catorcenal (cada 14 días)\n"
    "4️⃣ Semanal\n"
    "5️⃣ Otra frecuencia (tú me dices cuántas veces al año)"
)

MENSAJE_FRECUENCIA_EMPRENDEDOR = (
    "Antes de empezar, dime cada cuánto quieres analizar tu negocio (por ejemplo, cuánto vendes y gastas "
    "por semana, o por mes; tú eliges). Esto es importante porque, si más adelante me dices que tienes un "
    "crédito, el pago de ese crédito se va a calcular usando este MISMO periodo, para que todos los números "
    "cuadren entre sí.\n\n"
    "1️⃣ Mensual\n"
    "2️⃣ Quincenal (cada 15 días)\n"
    "3️⃣ Catorcenal (cada 14 días)\n"
    "4️⃣ Semanal\n"
    "5️⃣ Otro periodo (tú me dices cuántas veces al año se repite)"
)

FRECUENCIA_EMPRENDEDOR_FRASE = {
    "mensual": "al mes",
    "quincenal": "a la quincena",
    "catorcenal": "cada 14 días",
    "semanal": "a la semana",
    "personalizada": "en tu periodo elegido",
}

def calcular_plazo_y_tasa_periodo(anios, tasa_anual_pct, periodos_por_anio):
    """
    Convierte años + tasa anual (%) + frecuencia de pago en:
    - el número total de pagos (plazo)
    - la tasa de interés correspondiente a UN periodo de pago
    Así la persona nunca tiene que hacer esta conversión ella misma.
    """
    periodos_por_anio = Decimal(str(periodos_por_anio))
    plazo_total = int(
        (Decimal(str(anios)) * periodos_por_anio).to_integral_value(rounding=ROUND_HALF_UP)
    )
    tasa_periodo = (Decimal(str(tasa_anual_pct)) / Decimal("100")) / periodos_por_anio
    return plazo_total, tasa_periodo

def _calcular_y_resumir(contexto, tasa_anual_pct, anios, periodos_por_anio, frecuencia_label):
    plazo, tasa_periodo = calcular_plazo_y_tasa_periodo(anios, tasa_anual_pct, periodos_por_anio)
    monto = contexto["monto"]
    pago = calcular_pago_fijo_excel(monto, tasa_periodo, plazo)
    total_pagado = pago * plazo
    intereses = total_pagado - monto
    contexto["plazo"] = plazo
    contexto["tasa"] = tasa_periodo
    contexto["pago_fijo"] = pago
    contexto["frecuencia_label"] = frecuencia_label
    return pago, total_pagado, intereses, plazo

def _resolver_frecuencia_flujo1(contexto, frecuencia_label, periodos_por_anio):
    pago, total_pagado, intereses, plazo = _calcular_y_resumir(
        contexto, contexto["tasa_anual"], contexto["anios"], periodos_por_anio, frecuencia_label
    )
    contexto["esperando"] = "ver_si_abonos1"
    return (
        f"✅ Con esa frecuencia de pago, harías {plazo} pagos de ${pago:,.2f} cada uno.\n"
        f"💰 Pagarías en total: ${float(total_pagado):,.2f}\n"
        f"📉 De los cuales ${float(intereses):,.2f} serían intereses.\n\n"
        "¿Te gustaría ver cuánto podrías ahorrar si haces pagos extra a capital?\n"
        "Responde *sí* o *no*."
    )

def _resolver_frecuencia_flujo2(contexto, frecuencia_label, periodos_por_anio):
    pago, total_pagado, intereses, plazo = _calcular_y_resumir(
        contexto, contexto["tasa_anual"], contexto["anios"], periodos_por_anio, frecuencia_label
    )
    contexto["esperando"] = "abono_extra2"
    return (
        f"✅ Con esa frecuencia de pago, harías {plazo} pagos de ${pago:,.2f} cada uno.\n"
        f"💰 Pagarías en total: ${float(total_pagado):,.2f}\n"
        f"📉 De los cuales ${float(intereses):,.2f} serían intereses.\n\n"
        "¿Cuánto deseas abonar extra por periodo? (Ejemplo: 500)"
    )

def _resolver_frecuencia_monto_maximo(contexto, frecuencia_label, periodos_por_anio):
    plazo, tasa_periodo = calcular_plazo_y_tasa_periodo(
        contexto["anios_simular"], contexto["tasa_anual_simular"], periodos_por_anio
    )
    capacidad_mensual = contexto["capacidad_mensual"]
    capacidad_periodo = (capacidad_mensual * Decimal("12") / Decimal(str(periodos_por_anio))).quantize(Decimal("0.01"))

    base = Decimal("1") + tasa_periodo
    potencia = base ** plazo
    inverso = Decimal("1") / potencia
    factor = (Decimal("1") - inverso) / tasa_periodo
    monto_maximo = (capacidad_periodo * factor).quantize(Decimal("0.01"))

    contexto["monto_maximo"] = monto_maximo
    contexto["esperando"] = "submenu_despues_de_maximo"

    return (
        f"✅ Con esa frecuencia de pago, tu capacidad sería de ${capacidad_periodo:,.2f} por pago "
        f"(equivalente a tu límite mensual de ${capacidad_mensual:,.2f}).\n"
        f"Podrías aspirar a un crédito de hasta ${monto_maximo:,.2f} en {plazo} pagos.\n\n"
        "¿Te gustaría ahora validar un crédito específico o volver al menú?\n"
        "1. Validar un crédito\n"
        "2. Regresar al menú\n"
        "Escribe 1 o 2."
    )

def _resolver_frecuencia_deseado(contexto, frecuencia_label, periodos_por_anio):
    plazo, tasa_periodo = calcular_plazo_y_tasa_periodo(
        contexto["anios_deseado"], contexto["tasa_anual_deseada"], periodos_por_anio
    )
    monto = contexto["monto_deseado"]
    capacidad_mensual = contexto["capacidad_mensual"]
    capacidad_periodo = (capacidad_mensual * Decimal("12") / Decimal(str(periodos_por_anio))).quantize(Decimal("0.01"))
    porcentaje_riesgo = contexto["porcentaje_riesgo"]

    pago_estimado = calcular_pago_fijo_excel(monto, tasa_periodo, plazo)

    if pago_estimado <= capacidad_periodo:
        return (
            f"✅ Puedes pagar este crédito sin problemas.\n"
            f"Tu pago estimado por periodo es ${pago_estimado:,.2f}, dentro de tu capacidad "
            f"(${capacidad_periodo:,.2f} por pago con esa frecuencia).\n"
            "Escribe *menú* para volver."
        )
    else:
        diferencia = (pago_estimado - capacidad_periodo).quantize(Decimal("0.01"))
        incremento_ingreso = (diferencia / porcentaje_riesgo).quantize(Decimal("0.01"))
        reduccion_revolvente = (diferencia / Decimal("0.06")).quantize(Decimal("0.01"))
        return (
            f"❌ No podrías pagar este crédito con esa frecuencia.\n"
            f"Pago por periodo: ${pago_estimado:,.2f} > tu capacidad: ${capacidad_periodo:,.2f}.\n\n"
            "🔧 Opciones:\n"
            f"1. Reducir pagos fijos en al menos ${diferencia:,.2f} al mes.\n"
            f"2. Aumentar ingresos en ~${incremento_ingreso:,.2f} al mes.\n"
            f"3. Reducir deudas revolventes en ~${reduccion_revolvente:,.2f}.\n\n"
            "Escribe *menú* para volver."
        )

# =========================================
# Cálculo del ahorro con abonos extra
# =========================================
def calcular_ahorro_por_abonos(monto, tasa, plazo, abono_extra, desde_periodo):
    P = Decimal(str(monto))
    r = Decimal(str(tasa))
    n = int(plazo)
    abono = Decimal(str(abono_extra))
    desde = int(desde_periodo)

    pago_fijo = calcular_pago_fijo_excel(P, r, n)
    saldo = P
    periodo = 1
    intereses_totales = Decimal('0.00')
    pagos_realizados = 0
    ultimo_pago = Decimal('0.00')
    total_con_abonos = Decimal('0.00')

    while saldo > 0:
        interes = saldo * r
        abono_a_capital = pago_fijo - interes

        if periodo >= desde:
            abono_a_capital += abono
            total_pago_periodo = pago_fijo + abono
        else:
            total_pago_periodo = pago_fijo

        if abono_a_capital >= saldo:
            interes_final = saldo * r
            ultimo_pago = saldo + interes_final
            intereses_totales += interes_final
            total_con_abonos += ultimo_pago
            pagos_realizados += 1
            break

        saldo -= abono_a_capital
        intereses_totales += interes
        total_con_abonos += total_pago_periodo
        pagos_realizados += 1
        periodo += 1

    total_sin_abonos = pago_fijo * n
    ahorro_total = total_sin_abonos - total_con_abonos
    pagos_ahorrados = n - pagos_realizados

    return (
        total_sin_abonos.quantize(Decimal("0.01")),
        total_con_abonos.quantize(Decimal("0.01")),
        ahorro_total.quantize(Decimal("0.01")),
        pagos_ahorrados
    )

# =========================================
# Pago mínimo de tarjeta de crédito
# =========================================
# Regla vigente de Banco de México (Circular 13/2011, modificada por la
# 14/2014): el banco debe cobrar el MAYOR entre estos dos pisos, cada uno
# más los intereses generados en el periodo y el IVA de esos intereses. Los
# bancos pueden exigir un mínimo más alto, pero nunca uno menor a este piso.
def _calcular_pago_minimo_tarjeta(saldo, limite, tasa_anual):
    interes = saldo * tasa_anual / Decimal('12') / Decimal('100')
    iva = interes * Decimal('0.16')
    opcion1 = saldo * Decimal('0.015') + interes + iva
    opcion2 = limite * Decimal('0.0125') + interes + iva
    if opcion1 >= opcion2:
        pago_minimo, criterio = opcion1, 1
    else:
        pago_minimo, criterio = opcion2, 2
    tope = saldo + interes + iva
    if pago_minimo > tope:
        pago_minimo = tope
    return pago_minimo, interes, iva, criterio, opcion1, opcion2

def _simular_solo_pago_minimo(saldo, limite, tasa_anual, tope_meses=600):
    """
    Simula qué pasaría si una persona SOLO pagara el pago mínimo cada mes,
    mes tras mes, sin volver a usar la tarjeta. tope_meses es un límite de
    seguridad para no calcular indefinidamente en un caso extremo.
    """
    saldo_restante = saldo
    meses = 0
    total_pagado = Decimal('0.00')
    total_interes_iva = Decimal('0.00')
    while saldo_restante > Decimal('0.01') and meses < tope_meses:
        pago_minimo, interes, iva, _, _, _ = _calcular_pago_minimo_tarjeta(saldo_restante, limite, tasa_anual)
        abono_a_capital = pago_minimo - interes - iva
        if abono_a_capital <= 0:
            # No debería ocurrir con la fórmula oficial (siempre incluye un
            # porcentaje de capital), pero se evita así un ciclo infinito.
            break
        if abono_a_capital >= saldo_restante:
            pago_final = saldo_restante + interes + iva
            total_pagado += pago_final
            total_interes_iva += interes + iva
            saldo_restante = Decimal('0.00')
            meses += 1
            break
        saldo_restante -= abono_a_capital
        total_pagado += pago_minimo
        total_interes_iva += interes + iva
        meses += 1
    se_alcanzo_el_tope = saldo_restante > Decimal('0.01')
    return meses, total_pagado.quantize(Decimal('0.01')), total_interes_iva.quantize(Decimal('0.01')), se_alcanzo_el_tope

# =========================================
# Plan para pagar varias deudas (bola de nieve / avalancha)
# =========================================
def _simular_estrategia_deudas(deudas, extra_mensual, orden_indices, tope_meses=600):
    """
    Simula, mes a mes, pagar varias deudas a la vez: cada mes se cobra
    interés sobre el saldo restante de cada una, se paga el mínimo de todas
    las que sigan activas, y el dinero extra disponible (el abono extra que
    la persona puede dar, más los mínimos ya liberados de las deudas que se
    van terminando) se concentra en la deuda que esté primero en
    orden_indices. Así se refleja el efecto "bola de nieve": el abono
    disponible va creciendo conforme se liquida cada deuda.

    deudas: lista de dicts con 'saldo', 'tasa_anual' (%) y 'pago_minimo'.
    orden_indices: lista de índices de `deudas`, en el orden de prioridad
    en que se les debe dar el dinero extra.

    Devuelve: (meses_totales, total_pagado, total_intereses,
    meses_liquidacion_por_indice, se_alcanzo_el_tope)
    """
    # Se usa un contexto de Decimal aparte, con mucha más precisión que la
    # global (que en este archivo se deja en 17 dígitos para el cálculo de
    # "costo real de compras a plazos"). Cuando una deuda es impagable con
    # los datos dados, el saldo simulado crece exponencialmente durante
    # varios cientos de meses hasta el tope de seguridad, y puede rebasar
    # 17 dígitos mucho antes de llegar ahí; con más precisión evitamos que
    # eso truene el cálculo en vez de simplemente reportar "no se puede
    # pagar" con tope_meses alcanzado.
    with localcontext() as ctx:
        ctx.prec = 200

        n = len(deudas)
        saldos = [d["saldo"] for d in deudas]
        minimos = [d["pago_minimo"] for d in deudas]
        tasas = [d["tasa_anual"] for d in deudas]
        meses_liquidacion = [None] * n
        total_pagado = Decimal('0.00')
        total_interes = Decimal('0.00')
        meses = 0

        while any(s > Decimal('0.01') for s in saldos) and meses < tope_meses:
            meses += 1
            # 1) Se aplica el interés del mes a cada deuda todavía activa.
            for i in range(n):
                if saldos[i] > Decimal('0.01'):
                    interes_i = saldos[i] * tasas[i] / Decimal('12') / Decimal('100')
                    saldos[i] += interes_i
                    total_interes += interes_i

            # 2) El dinero disponible este mes es el abono extra fijo, más el
            # pago mínimo de cada deuda que ya esté en cero (ese dinero ya no
            # tiene a dónde ir, así que se suma al abono extra).
            dinero_disponible = extra_mensual
            for i in range(n):
                if saldos[i] > Decimal('0.01'):
                    pago = minimos[i] if minimos[i] < saldos[i] else saldos[i]
                    saldos[i] -= pago
                    total_pagado += pago
                else:
                    dinero_disponible += minimos[i]

            # 3) Todo el dinero disponible se concentra en la deuda de mayor
            # prioridad que siga activa; si sobra, pasa a la siguiente.
            for idx in orden_indices:
                if dinero_disponible <= 0:
                    break
                if saldos[idx] > Decimal('0.01'):
                    abono = dinero_disponible if dinero_disponible < saldos[idx] else saldos[idx]
                    saldos[idx] -= abono
                    dinero_disponible -= abono
                    total_pagado += abono

            for i in range(n):
                if meses_liquidacion[i] is None and saldos[i] <= Decimal('0.01'):
                    meses_liquidacion[i] = meses

        se_alcanzo_el_tope = any(s > Decimal('0.01') for s in saldos)
        # El redondeo final también se hace dentro del contexto ampliado
        # (necesita suficiente precisión disponible si el monto quedó
        # enorme, caso de una deuda impagable). Como red de seguridad
        # adicional, si aun así no se pudiera redondear (una tasa
        # descomunal metida por error, por ejemplo), se usa un redondeo
        # aproximado en vez de dejar que truene: en ese caso siempre se
        # alcanzó el tope de todos modos, así que el número exacto ya no
        # importa para el mensaje que se muestra.
        try:
            total_pagado = total_pagado.quantize(Decimal('0.01'))
        except Exception:
            total_pagado = Decimal(str(round(float(total_pagado), 2)))
        try:
            total_interes = total_interes.quantize(Decimal('0.01'))
        except Exception:
            total_interes = Decimal(str(round(float(total_interes), 2)))

    return (
        meses,
        total_pagado,
        total_interes,
        meses_liquidacion,
        se_alcanzo_el_tope,
    )

# =========================================
# Costo real de compras a pagos fijos
# =========================================
from decimal import Decimal, getcontext
import numpy_financial as np

getcontext().prec = 17  # Precisión tipo Excel

def calcular_costo_credito_tienda(precio_contado, pago_periodico, num_pagos, periodos_anuales):
    try:
        precio = Decimal(str(precio_contado))
        cuota = Decimal(str(pago_periodico))
        n = int(num_pagos)
        p = int(periodos_anuales)

        if precio <= 0 or cuota <= 0 or n <= 0 or p <= 0:
            raise ValueError("Todos los valores deben ser mayores a cero.")

        total_pagado = cuota * n
        intereses = total_pagado - precio

        # Cálculo de TIR (tasa efectiva por periodo)
        flujos = [-float(precio)] + [float(cuota)] * n
        tir = np.irr(flujos)

        if tir is None or tir <= -1:
            raise ValueError("No se pudo calcular la TIR correctamente.")

        tasa_periodo = Decimal(tir)
        tasa_anual = (Decimal("1") + tasa_periodo) ** Decimal(p) - Decimal("1")
        porcentaje_intereses = (intereses / precio) * Decimal("100")

        # Redondeo final
        total_pagado = total_pagado.quantize(Decimal("0.01"))
        intereses = intereses.quantize(Decimal("0.01"))
        porcentaje_intereses = porcentaje_intereses.quantize(Decimal("0.01"))
        tasa_periodo = (tasa_periodo * 100).quantize(Decimal("0.01"))
        tasa_anual = (tasa_anual * 100).quantize(Decimal("0.01"))

        return (
            f"📌 Resultados de tu compra a pagos fijos:\n"
            f"💰 Precio de contado: ${precio:,.2f}\n"
            f"📆 Pagos fijos de ${cuota:,.2f} durante {n} periodos.\n\n"
            f"💸 Total pagado: ${total_pagado:,.2f}\n"
            f"🧮 Intereses pagados: ${intereses:,.2f} (equivale al {porcentaje_intereses}% del precio de contado)\n"
            f"📈 Tasa por periodo: {tasa_periodo}%\n"
            f"📅 Tasa anual equivalente (basado en {p} periodos al año): {tasa_anual}%\n\n"
            "🔍 *Nota:* La tasa anual equivalente muestra cuánto crecería tu deuda si el interés se aplicara de forma compuesta todo el año. "
            "No significa que pagarás ese porcentaje exacto en dinero, pero sí te ayuda a comparar distintos créditos.\n\n"
            "Escribe *menú* para volver al inicio."
        )

    except Exception as e:
        return f"❌ Error al calcular: {e}"

# =========================================
# Ahorro: meta de ahorro
# =========================================
def calcular_ahorro_periodico(meta, ahorro_inicial, meses_totales, periodos_por_anio, frecuencia_label):
    """
    Dado cuánto quiere ahorrar una persona en total, cuánto tiene ya ahorrado,
    en cuánto tiempo (en meses) y con qué frecuencia puede apartar dinero,
    calcula cuánto necesita apartar en cada periodo. Es un cálculo simple,
    sin intereses (a diferencia de Inversión), porque Ahorro representa
    guardar dinero sin buscar que crezca.
    """
    try:
        meta = Decimal(str(meta))
        ahorro_inicial = Decimal(str(ahorro_inicial))
        meses_totales = Decimal(str(meses_totales))
        periodos_por_anio = Decimal(str(periodos_por_anio))

        if meta <= 0 or meses_totales <= 0 or periodos_por_anio <= 0:
            return (
                "Uy, algo no cuadró con esos datos 🤔 Revisa que los números sean mayores a cero "
                "e inténtalo de nuevo, o escribe *menú* para empezar otra vez."
            )
        if ahorro_inicial < 0:
            return "Ese número no puede ser negativo 🙂 Si no tienes nada ahorrado todavía, escribe 0."

        if ahorro_inicial >= meta:
            return (
                f"🎉 ¡Buenísima noticia! Ya tienes ${ahorro_inicial:,.2f} ahorrado, lo cual alcanza o "
                f"supera tu meta de ${meta:,.2f}. ¡No necesitas apartar nada más para lograrlo! 🙌\n\n"
                "Escribe *menú* para volver al inicio."
            )

        monto_faltante = meta - ahorro_inicial
        total_periodos = int(
            (meses_totales * periodos_por_anio / Decimal("12")).to_integral_value(rounding=ROUND_HALF_UP)
        )
        if total_periodos <= 0:
            total_periodos = 1

        aporte_por_periodo = (monto_faltante / Decimal(total_periodos)).quantize(Decimal("0.01"))

        return (
            "📌 Resultado de tu plan de ahorro:\n"
            f"💰 Meta: ${meta:,.2f}\n"
            f"🏦 Ya tienes ahorrado: ${ahorro_inicial:,.2f}\n"
            f"📉 Te falta ahorrar: ${monto_faltante:,.2f}\n"
            f"📆 Tiempo: {meses_totales} meses, ahorrando de forma {frecuencia_label} ({total_periodos} periodos)\n\n"
            f"✅ Necesitas apartar ${aporte_por_periodo:,.2f} en cada periodo para lograrlo.\n\n"
            "💡 Tip: si no sabes por dónde empezar, un buen primer objetivo es tener de 3 a 6 meses de "
            "tus gastos guardados, como colchón para emergencias.\n\n"
            "Escribe *menú* para volver al inicio."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"

# =========================================
# Inversión: crecimiento de una inversión
# =========================================
def calcular_crecimiento_inversion(monto_inicial, aportacion_periodica, anios, tasa_anual_pct, periodos_por_anio, frecuencia_label):
    """
    Dado un monto inicial (puede ser 0), una aportación periódica (puede ser 0),
    un rendimiento anual esperado, un plazo y una frecuencia de aportación,
    calcula cuánto crecería ese dinero. Reutiliza calcular_plazo_y_tasa_periodo
    para que la persona nunca tenga que convertir la tasa anual ella misma.
    A diferencia de Ahorro, aquí SÍ se asume un rendimiento (interés compuesto).
    """
    try:
        monto_inicial = Decimal(str(monto_inicial))
        aportacion_periodica = Decimal(str(aportacion_periodica))
        tasa_anual_pct = Decimal(str(tasa_anual_pct))

        if monto_inicial < 0 or aportacion_periodica < 0:
            return "Esos montos no pueden ser negativos 🙂 Si no vas a aportar nada al inicio o en cada periodo, escribe 0."
        if monto_inicial == 0 and aportacion_periodica == 0:
            return (
                "Para calcular el crecimiento necesito que aportes algo, ya sea al inicio o en cada "
                "periodo. Escribe *menú* para intentarlo de nuevo."
            )
        if tasa_anual_pct < 0:
            return "La tasa de rendimiento esperada no puede ser negativa para este cálculo 🙂 Indica un número positivo (ejemplo: 10)."

        plazo, tasa_periodo = calcular_plazo_y_tasa_periodo(anios, tasa_anual_pct, periodos_por_anio)
        if plazo <= 0:
            return "El tiempo debe ser mayor a cero. Escribe *menú* para intentarlo de nuevo."

        fv_inicial = monto_inicial * (Decimal("1") + tasa_periodo) ** plazo
        if tasa_periodo == 0:
            fv_aportaciones = aportacion_periodica * Decimal(plazo)
        else:
            fv_aportaciones = aportacion_periodica * (
                ((Decimal("1") + tasa_periodo) ** plazo - Decimal("1")) / tasa_periodo
            )

        fv_total = (fv_inicial + fv_aportaciones).quantize(Decimal("0.01"))
        total_aportado = (monto_inicial + aportacion_periodica * Decimal(plazo)).quantize(Decimal("0.01"))
        intereses_generados = (fv_total - total_aportado).quantize(Decimal("0.01"))

        return (
            "📌 Resultado de tu simulación de inversión:\n"
            f"💰 Monto inicial: ${monto_inicial:,.2f}\n"
            f"➕ Aportación por periodo: ${aportacion_periodica:,.2f}\n"
            f"📆 Tiempo: {plazo} periodos, aportando de forma {frecuencia_label}\n"
            f"📈 Rendimiento anual esperado: {tasa_anual_pct}%\n\n"
            f"🏦 Total que habrás puesto de tu bolsillo: ${total_aportado:,.2f}\n"
            f"✨ Lo que generaría el rendimiento: ${intereses_generados:,.2f}\n"
            f"🎯 Total estimado al final: ${fv_total:,.2f}\n\n"
            "🔍 *Nota:* Este cálculo asume que el rendimiento se mantiene constante todo el tiempo, lo cual "
            "no siempre pasa en la vida real (las inversiones pueden subir y bajar de valor). Úsalo como "
            "una referencia para comparar opciones, no como una promesa exacta.\n\n"
            "Escribe *menú* para volver al inicio."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"

# =========================================
# Jubilación: ahorro voluntario con la fórmula oficial de CONSAR/Afore
# =========================================
# Metodología tomada tal cual de la nota "¿Cómo funciona la calculadora de
# ahorro voluntario?" de CONSAR (la misma que usan las Afores para estimar
# el saldo y la pensión a partir de aportaciones voluntarias):
#
#   Sf = Si(1+r^(m))^n(1-c^(m))^n + Av^(m) [ ((1+r^(m))^n(1-c^(m))^n - 1) / ((1+r^(m))(1-c^(m)) - 1) ]
#
# donde r^(m) es el rendimiento mensual equivalente al rendimiento anual
# esperado, y c^(m) es la comisión mensual de las SIEFORE adicionales. A
# partir de Sf se estima la pensión mensual del primer año de un Retiro
# Programado: Mensualidad = Sf / (12 * URV).
#
# A diferencia de la anterior calculadora de "meta de ahorro" (que resolvía
# cuánto aportar para llegar a una meta), aquí se parte de una aportación
# mensual fija que la persona ya decidió, y se calcula a dónde llegaría con
# ella: es el mismo enfoque que la calculadora real de una Afore.

# Comisión anual promedio vigente de las SIEFORE adicionales, confirmada
# por Jazmín el 17/sep/2026. CONSAR la actualiza de vez en cuando: si ha
# pasado mucho tiempo desde esa fecha, conviene verificarla de nuevo en
# https://www.gob.mx/consar.
COMISION_ANUAL_SIEFORE_ADICIONALES = Decimal("0.57")  # %

# URV (Unidad de Renta Vitalicia) para un Ahorrador activo sin
# beneficiarios, vigente desde el 14 de septiembre de 2026 (la semana más
# reciente publicada al momento de programar esto), tomada del archivo
# oficial de CONSAR ("URV_2026.xls", hoja "2026_Activos"). La URV cambia
# CADA SEMANA: si ha pasado mucho tiempo desde esa fecha, hay que
# actualizar esta tabla con el archivo vigente de CONSAR (Anexo C de las
# Disposiciones de carácter general aplicables a los retiros programados).
URV_VIGENTE = {
    60: {"hombre": Decimal("12.769161862147167"), "mujer": Decimal("14.03584217632955")},
    65: {"hombre": Decimal("11.689588383089918"), "mujer": Decimal("12.85693833274364")},
    66: {"hombre": Decimal("11.46247101013117"), "mujer": Decimal("12.599302662761362")},
    67: {"hombre": Decimal("11.231904072367147"), "mujer": Decimal("12.334432614743635")},
}

def calcular_ahorro_voluntario_afore(saldo_actual, edad_actual, edad_retiro, genero, aportacion_mensual, tasa_anual_pct):
    """
    Implementa la fórmula oficial de CONSAR descrita arriba. edad_actual y
    edad_retiro se dan en años completos (edad_retiro debe ser 60, 65 o 67,
    las únicas edades para las que tenemos tabla de URV); n (meses que
    faltan) se aproxima como (edad_retiro - edad_actual) * 12.
    """
    try:
        saldo_actual = Decimal(str(saldo_actual))
        aportacion_mensual = Decimal(str(aportacion_mensual))
        tasa_anual_pct = Decimal(str(tasa_anual_pct))

        n = (edad_retiro - edad_actual) * 12
        if n <= 0:
            return (
                "Tu edad de retiro debe ser mayor a tu edad actual. Escribe *menú* para intentarlo de nuevo "
                "con otros datos."
            )

        r_anual = tasa_anual_pct / Decimal("100")
        r_m = (Decimal("1") + r_anual) ** (Decimal("1") / Decimal("12")) - Decimal("1")
        c_m = (COMISION_ANUAL_SIEFORE_ADICIONALES / Decimal("100")) / Decimal("12")

        factor_combinado = (Decimal("1") + r_m) ** n * (Decimal("1") - c_m) ** n
        total_por_saldo_actual = saldo_actual * factor_combinado

        # tr^(m) = (1+r^(m))(1-c^(m)) - 1: así lo define textualmente la
        # metodología oficial de CONSAR (nota: es 1 MENOS c^(m), no más,
        # en este denominador).
        denominador = (Decimal("1") + r_m) * (Decimal("1") - c_m) - Decimal("1")
        if denominador == 0:
            total_por_aportaciones = aportacion_mensual * Decimal(n)
        else:
            total_por_aportaciones = aportacion_mensual * (factor_combinado - Decimal("1")) / denominador

        saldo_final = (total_por_saldo_actual + total_por_aportaciones).quantize(Decimal("0.01"))

        urv = URV_VIGENTE[edad_retiro][genero]
        pension_mensual = (saldo_final / (Decimal("12") * urv)).quantize(Decimal("0.01"))

        total_aportado_bolsillo = (aportacion_mensual * Decimal(n)).quantize(Decimal("0.01"))
        rendimiento_generado = (saldo_final - saldo_actual - total_aportado_bolsillo).quantize(Decimal("0.01"))

        return (
            "📌 Resultado de tu ahorro voluntario para el retiro:\n"
            f"🏦 Saldo actual de tu cuenta individual: ${saldo_actual:,.2f}\n"
            f"📆 Edad actual: {edad_actual} años · Edad de retiro: {edad_retiro} años ({n} meses)\n"
            f"💵 Aportación voluntaria mensual: ${aportacion_mensual:,.2f}\n"
            f"📈 Rendimiento anual esperado: {tasa_anual_pct}%\n"
            f"🏷️ Comisión anual de SIEFORE adicionales: {COMISION_ANUAL_SIEFORE_ADICIONALES}%\n\n"
            f"✅ Saldo estimado al retiro: ${saldo_final:,.2f}\n"
            f"🧮 De ese total, ${total_aportado_bolsillo:,.2f} saldría de tus aportaciones mensuales (sin "
            f"contar tu saldo actual) y ${rendimiento_generado:,.2f} vendría del rendimiento generado, ya "
            "descontando comisiones.\n\n"
            f"💰 Pensión mensual estimada (primer año de un Retiro Programado): ${pension_mensual:,.2f}\n\n"
            "🔍 *Nota:* Este es un cálculo aproximado. Para un resultado 100% exacto, verifica en la "
            "calculadora oficial de CONSAR: https://www.consar.gob.mx/gobmx/aplicativo/calculadora/Calculadoras/\n\n"
            "Escribe *menú* para volver al inicio."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"


# =========================================
# Jubilación: calculadoras de IMSS e ISSSTE (versión simplificada)
# =========================================
# Jazmín pidió reestructurar la opción 1 de Jubilación en "Calculadoras de
# jubilación", con un submenú que ofrece una calculadora distinta según
# dónde cotiza la persona (IMSS, ISSSTE o independiente), tal como en la
# página oficial de CONSAR (https://www.gob.mx/consar). Nos dio la
# metodología oficial completa de las 3 (documentos "Metodología de la
# Calculadora..." de CONSAR/Hacienda, 2026).
#
# La metodología COMPLETA de IMSS y de ISSSTE incluye, además del saldo y
# la pensión, una Pensión Garantizada (piso mínimo legal), un Complemento
# Solidario del Fondo de Pensiones para el Bienestar, y en el caso de IMSS
# un calendario legal de aumento gradual a las aportaciones obligatorias
# hasta 2030 (con una fórmula de anualidad de dos tramos bastante compleja)
# y una tabla actuarial de "anualidad contingente" (distinta a la URV) para
# la pensión, que no es de acceso público en un formato simple.
#
# Jazmín decidió explícitamente ir por una versión SIMPLIFICADA para IMSS
# e ISSSTE (con la leyenda de que es una aproximación):
#   - El SALDO acumulado sí sigue la fórmula oficial con sus aportaciones
#     obligatorias (y, en el caso de IMSS, el ahorro voluntario adicional),
#     pero usando la tasa de aportación VIGENTE EN 2026 de forma constante
#     durante todo el periodo (sin el calendario gradual hasta 2030). Esto
#     probablemente SUBESTIMA un poco el saldo real, ya que las tasas reales
#     subirán con el tiempo.
#   - La comisión c^(m) que usa la metodología oficial de IMSS/ISSSTE es la
#     comisión general de la Afore (no específicamente la de las SIEFORE
#     adicionales); como no tenemos ese dato por separado, reutilizamos
#     COMISION_ANUAL_SIEFORE_ADICIONALES para toda la cuenta.
#   - La PENSIÓN mensual se estima con la misma URV que ya usamos (como en
#     independientes), en lugar de la tabla de anualidad contingente.
#   - No se incluye Pensión Garantizada ni Complemento Solidario del FPB:
#     el monto mostrado es la estimación "de mercado", sin esas salvaguardas
#     legales mínimas.
#   - Para IMSS, el rango salarial (de qué tanto se aporta según el sueldo)
#     se calcula automáticamente a partir del salario que la persona ya
#     dio, sin preguntarle nada más. OJO: en 2026 el salario mínimo diario
#     ($315.04) ya es más alto que 1.5 UMA ($175.97), así que el rango 2
#     ("de 1.01 salarios mínimos a 1.5 UMA") nunca se puede asignar este
#     año, y alguien que gane justo el salario mínimo puede terminar en un
#     rango más alto (con una aportación mayor a la que le tocaría en
#     estricto sentido legal) en vez del rango 1. Jazmín revisó este
#     problema el 19/sep/2026 y decidió explícitamente automatizarlo sin
#     pedirle confirmación a la persona (antes se le pedía elegir su
#     rango de una lista, pero resultaba confuso para el público objetivo
#     del bot), aceptando ese riesgo conocido a cambio de un flujo más
#     simple. Ver _imss_rango_salarial_desde_salario() más abajo.
#
# Tabla de aportación obligatoria total (retiro + cesantía y vejez del
# patrón, del trabajador, y de retiro) vigente en 2026 por rango salarial
# (tomada tal cual de la metodología oficial de CONSAR para IMSS).
IMSS_APORTACION_2026 = {
    1: Decimal("6.275"),
    2: Decimal("6.801"),
    3: Decimal("7.976"),
    4: Decimal("8.681"),
    5: Decimal("9.151"),
    6: Decimal("9.486"),
    7: Decimal("9.738"),
    8: Decimal("10.638"),
}

# Cuota social diaria (pesos) por rango salarial, vigente para el periodo
# enero-abril de 2026 (se actualiza trimestralmente con el INPC, conforme
# al Art. 168, fracción IV, de la Ley del Seguro Social). Para obtener el
# valor mensual se multiplica por 30.4, tal como indica la metodología.
IMSS_CUOTA_SOCIAL_DIARIA = {
    1: Decimal("12.15794"),
    2: Decimal("11.30970"),
    3: Decimal("10.46148"),
    4: Decimal("9.61325"),
    5: Decimal("8.76502"),
    6: Decimal("7.91682"),
    7: Decimal("7.06856"),
    8: Decimal("0.00000"),
}

IMSS_RANGOS_SALARIALES_TEXTO = {
    1: "Hasta 1 salario mínimo",
    2: "De 1.01 salarios mínimos a 1.5 UMA",
    3: "De 1.51 a 2 UMA",
    4: "De 2.01 a 2.5 UMA",
    5: "De 2.51 a 3 UMA",
    6: "De 3.01 a 3.5 UMA",
    7: "De 3.51 a 4 UMA",
    8: "Más de 4 UMA",
}

# Salario mínimo general diario vigente en 2026 (usado únicamente para
# ubicar el rango salarial 1 de la tabla de arriba). Cambia cada enero,
# por decreto de la Comisión Nacional de los Salarios Mínimos: si ha
# pasado mucho tiempo desde que se fijó este valor, conviene verificarlo
# en https://www.gob.mx/conasami.
SALARIO_MINIMO_DIARIO_2026 = Decimal("315.04")


def _imss_rango_salarial_desde_salario(salario_mensual):
    """
    Calcula automáticamente el rango salarial (1 a 8) de la tabla de
    aportación obligatoria del IMSS a partir del salario mensual de la
    persona, aplicando los cortes oficiales en el orden en que la ley
    los define: primero se compara contra el salario mínimo (rango 1) y
    luego, si no aplica, contra los múltiplos de UMA de los rangos 2 a 8.

    Jazmín decidió automatizar este paso (en vez de pedirle a la persona
    que elija su rango de una lista, que resultaba confuso) sabiendo que
    en 2026 el rango 2 nunca se asigna, porque el salario mínimo diario
    ya es más alto que su límite superior en UMA (ver nota en la sección
    de arriba). Esto puede asignarle a alguien con el salario mínimo (o
    cercano) un rango más alto del que le correspondería en estricto
    sentido legal.
    """
    salario_diario = Decimal(str(salario_mensual)) / Decimal("30.4")
    if salario_diario <= SALARIO_MINIMO_DIARIO_2026:
        return 1
    limites_uma_por_rango = [
        (2, Decimal("1.5")),
        (3, Decimal("2.0")),
        (4, Decimal("2.5")),
        (5, Decimal("3.0")),
        (6, Decimal("3.5")),
        (7, Decimal("4.0")),
    ]
    for rango, multiplo_uma in limites_uma_por_rango:
        if salario_diario <= multiplo_uma * UMA_DIARIA_VIGENTE:
            return rango
    return 8


# Aportación obligatoria del trabajador al ISSSTE por concepto de Retiro,
# Cesantía en edad avanzada y Vejez (no varía por nivel salarial).
ISSSTE_APORTACION_OBLIGATORIA_PCT = Decimal("11.3")

# Cuota social diaria (pesos) del ISSSTE: NO varía por nivel salarial (a
# diferencia de la de IMSS). Es el 5.5% del salario mínimo general para el
# Distrito Federal vigente al 1 de julio de 1997 (un valor histórico fijo,
# distinto del salario mínimo actual), actualizada trimestralmente (marzo,
# junio, septiembre y diciembre) conforme al INPC. Valor vigente para el
# periodo de junio de 2026, confirmado por Jazmín el 19/sep/2026 con la
# nota metodológica oficial de PENSIONISSSTE: https://www.pensionissste.gob.mx/Calculadora/resources/excel/Nota%20Metodologica_ISSSTE_sep2026.pdf
# Si ha pasado mucho tiempo desde esa fecha, conviene revisar si ya se
# actualizó (cambia cada trimestre).
ISSSTE_CUOTA_SOCIAL_DIARIA = Decimal("6.7559")

# Valor máximo del "peso" combinado (aportación del trabajador + la que
# aporta el Gobierno Federal) para el ahorro solidario del ISSSTE, como %
# del sueldo básico mensual.
ISSSTE_AHORRO_SOLIDARIO_TOPE_GOBIERNO_PCT = Decimal("6.5")
ISSSTE_AHORRO_SOLIDARIO_MULTIPLICADOR_GOBIERNO = Decimal("3.25")


def _fv_anualidad(pago_mensual, tr_m, n):
    """
    Valor futuro de una anualidad ordinaria de n pagos mensuales iguales
    (pago_mensual), con una tasa de crecimiento constante por periodo tr_m:
    pago * [(1+tr_m)^n - 1] / tr_m (o pago*n si tr_m es 0). Es la misma
    forma que usa la metodología oficial para acumular aportaciones
    periódicas (obligatorias o voluntarias).
    """
    if tr_m == 0:
        return pago_mensual * Decimal(n)
    factor = (Decimal("1") + tr_m) ** n
    return pago_mensual * (factor - Decimal("1")) / tr_m


def calcular_jubilacion_imss(
    saldo_actual, edad_actual, edad_retiro, genero, salario_mensual, rango_salarial,
    rendimiento_anual_pct, aportacion_voluntaria_mensual,
):
    """
    Versión simplificada (ver nota arriba) de la metodología oficial de
    CONSAR para trabajadores que cotizan al IMSS bajo el régimen de Ley 97.
    """
    try:
        saldo_actual = Decimal(str(saldo_actual))
        salario_mensual = Decimal(str(salario_mensual))
        aportacion_voluntaria_mensual = Decimal(str(aportacion_voluntaria_mensual))
        rendimiento_anual_pct = Decimal(str(rendimiento_anual_pct))

        n = (edad_retiro - edad_actual) * 12
        if n <= 0:
            return (
                "Tu edad de retiro debe ser mayor a tu edad actual. Escribe *menú* para intentarlo de nuevo "
                "con otros datos."
            )

        r_anual = rendimiento_anual_pct / Decimal("100")
        r_m = (Decimal("1") + r_anual) ** (Decimal("1") / Decimal("12")) - Decimal("1")
        c_m = (COMISION_ANUAL_SIEFORE_ADICIONALES / Decimal("100")) / Decimal("12")
        tr_m = (Decimal("1") + r_m) * (Decimal("1") - c_m) - Decimal("1")

        factor_saldo_actual = (Decimal("1") + tr_m) ** n
        total_por_saldo_actual = saldo_actual * factor_saldo_actual

        d = Decimal("0.80")  # densidad de cotización supuesta (80%)
        aportacion_obligatoria_pct = IMSS_APORTACION_2026[rango_salarial]
        cuota_social_mensual = IMSS_CUOTA_SOCIAL_DIARIA[rango_salarial] * Decimal("30.4")
        aportacion_obligatoria_mensual = (
            (aportacion_obligatoria_pct / Decimal("100")) * salario_mensual + cuota_social_mensual
        )

        total_obligatorio = _fv_anualidad(aportacion_obligatoria_mensual, tr_m, n)
        total_voluntario = _fv_anualidad(aportacion_voluntaria_mensual, tr_m, n)
        total_aportaciones = d * (total_obligatorio + total_voluntario)

        saldo_final = (total_por_saldo_actual + total_aportaciones).quantize(Decimal("0.01"))

        urv = URV_VIGENTE[edad_retiro][genero]
        pension_mensual = (saldo_final / (Decimal("12") * urv)).quantize(Decimal("0.01"))

        return (
            "📌 Resultado de tu calculadora IMSS (Régimen de Ley 97):\n"
            f"🏦 Saldo actual de tu cuenta individual: ${saldo_actual:,.2f}\n"
            f"📆 Edad actual: {edad_actual} años · Edad de retiro: {edad_retiro} años ({n} meses)\n"
            f"💼 Salario mensual base de cotización: ${salario_mensual:,.2f} · Rango: "
            f"{IMSS_RANGOS_SALARIALES_TEXTO[rango_salarial]} ({aportacion_obligatoria_pct}%)\n"
            f"➕ Aportación voluntaria mensual: ${aportacion_voluntaria_mensual:,.2f}\n"
            f"📈 Rendimiento anual esperado: {rendimiento_anual_pct}%\n\n"
            f"✅ Saldo estimado al retiro: ${saldo_final:,.2f}\n"
            f"💰 Pensión mensual estimada (primer año de un Retiro Programado): ${pension_mensual:,.2f}\n\n"
            "🔍 *Nota:* Este es un cálculo aproximado. Para un resultado 100% exacto, verifica en la "
            "calculadora oficial de CONSAR: https://www.consar.gob.mx/gobmx/aplicativo/calculadora/Calculadoras/\n\n"
            "Escribe *menú* para volver al inicio."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"


def calcular_jubilacion_issste(
    saldo_actual, edad_actual, edad_retiro, genero, sueldo_basico_mensual,
    ahorro_solidario_pct, bono_pension, rendimiento_anual_pct,
):
    """
    Versión simplificada (ver nota arriba) de la metodología oficial de
    CONSAR para trabajadores que cotizan al ISSSTE bajo el régimen de
    cuentas individuales.
    """
    try:
        saldo_actual = Decimal(str(saldo_actual))
        sueldo_basico_mensual = Decimal(str(sueldo_basico_mensual))
        bono_pension = Decimal(str(bono_pension))
        rendimiento_anual_pct = Decimal(str(rendimiento_anual_pct))
        ahorro_solidario_pct = Decimal(str(ahorro_solidario_pct))

        n = (edad_retiro - edad_actual) * 12
        if n <= 0:
            return (
                "Tu edad de retiro debe ser mayor a tu edad actual. Escribe *menú* para intentarlo de nuevo "
                "con otros datos."
            )

        r_anual = rendimiento_anual_pct / Decimal("100")
        r_m = (Decimal("1") + r_anual) ** (Decimal("1") / Decimal("12")) - Decimal("1")
        c_m = (COMISION_ANUAL_SIEFORE_ADICIONALES / Decimal("100")) / Decimal("12")
        tr_m = (Decimal("1") + r_m) * (Decimal("1") - c_m) - Decimal("1")

        factor_saldo_actual = (Decimal("1") + tr_m) ** n
        total_por_saldo_actual = saldo_actual * factor_saldo_actual

        d = Decimal("0.80")  # densidad de cotización supuesta (80%)
        ao = (ISSSTE_APORTACION_OBLIGATORIA_PCT / Decimal("100")) * sueldo_basico_mensual

        aporte_trabajador_solidario = (ahorro_solidario_pct / Decimal("100")) * sueldo_basico_mensual
        aporte_gobierno_solidario = min(
            ISSSTE_AHORRO_SOLIDARIO_MULTIPLICADOR_GOBIERNO * aporte_trabajador_solidario,
            (ISSSTE_AHORRO_SOLIDARIO_TOPE_GOBIERNO_PCT / Decimal("100")) * sueldo_basico_mensual,
        )
        as_ = aporte_trabajador_solidario + aporte_gobierno_solidario

        cs = ISSSTE_CUOTA_SOCIAL_DIARIA * Decimal("30.4")

        total_obligatorio_y_solidario = d * _fv_anualidad(ao + as_ + cs, tr_m, n)
        total_bono = bono_pension * ((Decimal("1") + tr_m) ** n)

        saldo_final = (total_por_saldo_actual + total_obligatorio_y_solidario + total_bono).quantize(Decimal("0.01"))

        urv = URV_VIGENTE[edad_retiro][genero]
        pension_mensual = (saldo_final / (Decimal("12") * urv)).quantize(Decimal("0.01"))

        return (
            "📌 Resultado de tu calculadora ISSSTE (Régimen de cuentas individuales):\n"
            f"🏦 Saldo actual de tu cuenta individual: ${saldo_actual:,.2f}\n"
            f"📆 Edad actual: {edad_actual} años · Edad de retiro: {edad_retiro} años ({n} meses)\n"
            f"💼 Sueldo básico mensual: ${sueldo_basico_mensual:,.2f}\n"
            f"➕ Ahorro solidario: {ahorro_solidario_pct}% de tu sueldo (más lo que aporta el Gobierno Federal)\n"
            f"🎁 Bono de Pensión ISSSTE: ${bono_pension:,.2f}\n"
            f"📈 Rendimiento anual esperado: {rendimiento_anual_pct}%\n\n"
            f"✅ Saldo estimado al retiro: ${saldo_final:,.2f}\n"
            f"💰 Pensión mensual estimada (primer año de un Retiro Programado): ${pension_mensual:,.2f}\n\n"
            "🔍 *Nota:* Este es un cálculo aproximado. Para un resultado 100% exacto, verifica en la "
            "calculadora oficial de CONSAR: https://www.consar.gob.mx/gobmx/aplicativo/calculadora/Calculadoras/\n\n"
            "Escribe *menú* para volver al inicio."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"


# =========================================
# Jubilación: calculadora de pensión Ley 73 (IMSS, régimen anterior a 1997)
# =========================================
# A diferencia de Ley 97/ISSSTE/independientes (que acumulan un saldo en
# una cuenta individual y de ahí se estima una pensión), la Ley 73 es un
# esquema de beneficio definido: la pensión se calcula directamente con una
# fórmula legal, sin cuenta ni rendimiento de por medio. Metodología tomada
# de la nota "Metodología para clientes de la calculadora de Retiro" de
# Profuturo (Afore), que Jazmín compartió el 19/sep/2026 (la CONSAR no
# publica una propia para este régimen):
# https://www.profuturo.mx/content/dam/privado-afore/calculadora/Anexo_Ley73.pdf
#
#   k = salario promedio / (UMA * 30.4)               [en múltiplos de UMA]
#   CBA = salario promedio diario * %CBA * 365          [Cuantía Básica Anual]
#   años posteriores = (semanas cotizadas - 500) / 52
#   incremento anual = salario promedio diario * %incremento * 365 * años posteriores
#   CAP = CBA + incremento anual                        [Cuantía Anual de la Pensión]
#   pensión mensual = CAP * (1+15%) * (1+11%) * %pensión / 12
#
# %CBA y %incremento dependen del rango de k (tabla oficial); %pensión
# depende de la edad de retiro (60 a 65 años). El +15% y el +11% ya vienen
# incluidos tal cual en la fórmula de Profuturo (ayuda asistencial y el
# "Factor Fox" de un decreto de 2001); no dependen de si tienes o no
# dependientes económicos, así que no le preguntamos eso a la persona.
#
# La UMA diaria (usada para ubicar el rango de k) también viene de ese
# mismo documento. Cambia cada febrero: si ha pasado mucho tiempo, conviene
# verificarla en https://www.inegi.org.mx/temas/uma/.
UMA_DIARIA_VIGENTE = Decimal("117.31")

# (k_min, k_max, %CBA, %incremento anual). Las primeras 19 filas (hasta
# k=5.50) vienen de la tabla oficial de Profuturo. Jazmín encontró y envió
# el 22/sep/2026 un documento adicional ("Tablas y procedimiento para
# cálculo de pensión", que cita el art. 167 LSS 1973) con la tabla COMPLETA
# del artículo 167, incluyendo 3 renglones que a Profuturo no le cabían:
# de 5.51 a 5.75, de 5.76 a 6.00, y de 6.01 en adelante (sin tope numérico
# publicado, por lo que aquí lo dejamos prácticamente sin límite superior).
# Con esos 3 renglones ya no hay ningún salario que se quede sin cálculo.
#
# OJO con las unidades: ese documento describe la tabla "en veces el
# salario mínimo general vigente", no en UMA. No es un error: es la
# redacción ORIGINAL del artículo 167 (de antes de 2016, cuando la UMA no
# existía). Con la desindexación del salario mínimo de 2016, este tipo de
# referencias se convirtieron a UMA usando el valor que tenían salario
# mínimo y UMA en ese momento (cuando ambos arrancaron iguales), y desde
# entonces la UMA dejó de subir al mismo ritmo que el salario mínimo. Por
# eso Profuturo (un documento más reciente) presenta los mismos 19
# renglones pero ya etiquetados "en múltiplos de UMA": son la misma tabla,
# solo que con la unidad correcta y actualizada. Aquí seguimos usando UMA
# (variable k, definida abajo) para los 22 renglones, con toda confianza.
#
# Los porcentajes de los primeros 19 renglones coinciden entre ambos
# documentos, CON UNA EXCEPCIÓN: para el renglón "de 1.26 a 1.50", Profuturo
# dice 55.18% de cuantía básica y este nuevo documento dice 58.18%. Nos
# quedamos con el 55.18% de Profuturo (un documento oficial de una Afore
# regulada, para un producto real) por ser la fuente más confiable, y
# porque el patrón de un solo dígito distinto (5 vs. 8) es consistente con
# un error de transcripción en el otro documento; calculamx.com, que ya
# habíamos descartado antes en esta misma sesión por informacion poco
# confiable, tenía ese mismo 58.18% para ese renglón, lo que refuerza la
# sospecha de que es un error que se fue copiando de una fuente a otra.
LEY73_TABLA_CBA_INCREMENTO = [
    (Decimal("0"), Decimal("1.00"), Decimal("80.00"), Decimal("0.563")),
    (Decimal("1.01"), Decimal("1.25"), Decimal("77.11"), Decimal("0.814")),
    (Decimal("1.26"), Decimal("1.50"), Decimal("55.18"), Decimal("1.178")),
    (Decimal("1.51"), Decimal("1.75"), Decimal("49.23"), Decimal("1.430")),
    (Decimal("1.76"), Decimal("2.00"), Decimal("42.67"), Decimal("1.615")),
    (Decimal("2.01"), Decimal("2.25"), Decimal("37.65"), Decimal("1.756")),
    (Decimal("2.26"), Decimal("2.50"), Decimal("33.68"), Decimal("1.868")),
    (Decimal("2.51"), Decimal("2.75"), Decimal("30.48"), Decimal("1.958")),
    (Decimal("2.76"), Decimal("3.00"), Decimal("27.83"), Decimal("2.033")),
    (Decimal("3.01"), Decimal("3.25"), Decimal("25.60"), Decimal("2.096")),
    (Decimal("3.26"), Decimal("3.50"), Decimal("23.70"), Decimal("2.149")),
    (Decimal("3.51"), Decimal("3.75"), Decimal("22.07"), Decimal("2.195")),
    (Decimal("3.76"), Decimal("4.00"), Decimal("20.65"), Decimal("2.235")),
    (Decimal("4.01"), Decimal("4.25"), Decimal("19.39"), Decimal("2.271")),
    (Decimal("4.26"), Decimal("4.50"), Decimal("18.29"), Decimal("2.302")),
    (Decimal("4.51"), Decimal("4.75"), Decimal("17.30"), Decimal("2.330")),
    (Decimal("4.76"), Decimal("5.00"), Decimal("16.41"), Decimal("2.355")),
    (Decimal("5.01"), Decimal("5.25"), Decimal("15.61"), Decimal("2.377")),
    (Decimal("5.26"), Decimal("5.50"), Decimal("14.88"), Decimal("2.398")),
    (Decimal("5.51"), Decimal("5.75"), Decimal("14.22"), Decimal("2.416")),
    (Decimal("5.76"), Decimal("6.00"), Decimal("13.62"), Decimal("2.433")),
    (Decimal("6.01"), Decimal("999999"), Decimal("13.00"), Decimal("2.450")),
]

LEY73_PORCENTAJE_PENSION_POR_EDAD = {
    60: Decimal("75"), 61: Decimal("80"), 62: Decimal("85"),
    63: Decimal("90"), 64: Decimal("95"), 65: Decimal("100"),
}


def calcular_jubilacion_ley73(salario_promedio_mensual, semanas_cotizadas, edad_retiro):
    try:
        salario_promedio_mensual = Decimal(str(salario_promedio_mensual))
        semanas_cotizadas = Decimal(str(semanas_cotizadas))

        if semanas_cotizadas < 500:
            return (
                "Para este tipo de pensión se necesitan al menos 500 semanas cotizadas al IMSS, y tú "
                f"indicaste {semanas_cotizadas}. Puedes consultar tus semanas cotizadas exactas en la app "
                "del IMSS Digital. Escribe *menú* para intentarlo de nuevo con otro número."
            )

        salario_promedio_diario = salario_promedio_mensual / Decimal("30.4")
        k = salario_promedio_diario / UMA_DIARIA_VIGENTE

        bracket = None
        for k_min, k_max, pct_cba, pct_incremento in LEY73_TABLA_CBA_INCREMENTO:
            if k_min <= k <= k_max:
                bracket = (k_min, k_max, pct_cba, pct_incremento)
                break

        if bracket is None:
            return (
                f"Ese salario mensual parece un error de captura ({k:.2f} veces la UMA es un nivel "
                "extremadamente alto). Revisa que lo hayas escrito bien (ejemplo: 15000, sin ceros de más) "
                "y vuelve a intentarlo, o escribe *menú* para salir."
            )

        _, _, pct_cba, pct_incremento = bracket
        cba_anual = salario_promedio_diario * (pct_cba / Decimal("100")) * Decimal("365")
        anios_posteriores = (semanas_cotizadas - Decimal("500")) / Decimal("52")
        incremento_anual = (
            salario_promedio_diario * (pct_incremento / Decimal("100")) * Decimal("365") * anios_posteriores
        )
        cap_anual = cba_anual + incremento_anual

        pct_pension = LEY73_PORCENTAJE_PENSION_POR_EDAD[edad_retiro]
        pension_mensual = (
            cap_anual * Decimal("1.15") * Decimal("1.11") * (pct_pension / Decimal("100")) / Decimal("12")
        ).quantize(Decimal("0.01"))

        tasa_reemplazo = (pension_mensual / salario_promedio_mensual * Decimal("100")).quantize(Decimal("0.1"))

        return (
            "📌 Resultado de tu calculadora de pensión Ley 73 (IMSS):\n"
            f"💼 Salario mensual promedio de tus últimas 250 semanas: ${salario_promedio_mensual:,.2f}\n"
            f"📆 Semanas cotizadas: {semanas_cotizadas} (años después del mínimo de 500: "
            f"{anios_posteriores:.1f})\n"
            f"🎂 Edad de retiro: {edad_retiro} años ({pct_pension}% de la pensión completa)\n\n"
            f"💰 Pensión mensual estimada: ${pension_mensual:,.2f}\n"
            f"📊 Tasa de reemplazo (respecto a tu salario): {tasa_reemplazo}%\n\n"
            "🔍 *Nota:* Este es un cálculo aproximado, no el trámite oficial. Si tu resultado queda cerca "
            "del salario mínimo, tu pensión real podría ser más alta gracias a la Pensión Mínima "
            "Garantizada. Para un número exacto, haz tu trámite directamente con el IMSS.\n\n"
            "Escribe *menú* para volver al inicio."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"


# =========================================
# Herramientas para el emprendedor: precio de venta y punto de equilibrio
# =========================================
def calcular_precio_sugerido_emprendedor(unidades, costo_unitario, costos_fijos, utilidad_deseada):
    """
    Calculadora simplificada de precio de venta por costeo (costo más margen):
    precio = costo por unidad + (costos fijos + utilidad deseada) / unidades.

    Es una versión simplificada de un modelo de fijación de precios más completo
    (que también podría incluir depreciación, financiamiento e impuestos); aquí
    se omiten esos elementos a propósito para que la calculadora sea rápida de
    usar por WhatsApp. Si el negocio tiene un préstamo, se le sugiere a la
    persona incluir el pago mensual dentro de sus costos fijos.
    """
    unidades = Decimal(str(unidades))
    costo_unitario = Decimal(str(costo_unitario))
    costos_fijos = Decimal(str(costos_fijos))
    utilidad_deseada = Decimal(str(utilidad_deseada))

    precio_sugerido = (costo_unitario + (costos_fijos + utilidad_deseada) / unidades).quantize(Decimal("0.01"))
    return precio_sugerido

def calcular_resultado_precio_emprendedor(unidades, costo_unitario, costos_fijos, precio_elegido):
    """
    A partir de un precio de venta (sugerido o elegido por la persona), calcula
    el punto de equilibrio en unidades y la utilidad estimada si vende la
    cantidad de unidades que había planeado.
    """
    try:
        unidades = Decimal(str(unidades))
        costo_unitario = Decimal(str(costo_unitario))
        costos_fijos = Decimal(str(costos_fijos))
        precio_elegido = Decimal(str(precio_elegido))

        margen_unitario = precio_elegido - costo_unitario
        punto_equilibrio = (costos_fijos / margen_unitario).to_integral_value(rounding=ROUND_CEILING)
        utilidad_estimada = (margen_unitario * unidades - costos_fijos).quantize(Decimal("0.01"))

        if utilidad_estimada >= 0:
            linea_utilidad = (
                f"✅ Si vendes las {unidades:,.0f} unidades que planeaste a ${precio_elegido:,.2f} cada una, "
                f"tendrías una utilidad estimada de ${utilidad_estimada:,.2f}."
            )
        else:
            linea_utilidad = (
                f"⚠️ Si vendes las {unidades:,.0f} unidades que planeaste a ${precio_elegido:,.2f} cada una, "
                f"tendrías una pérdida estimada de ${abs(utilidad_estimada):,.2f}, porque no alcanzarías tu "
                "punto de equilibrio."
            )

        return (
            "📌 Resultado con el precio que elegiste:\n"
            f"💲 Precio por unidad: ${precio_elegido:,.2f}\n"
            f"🧮 Costo por unidad: ${costo_unitario:,.2f}\n"
            f"📉 Costos fijos en tu periodo: ${costos_fijos:,.2f}\n\n"
            f"⚖️ Punto de equilibrio: necesitas vender {punto_equilibrio:,.0f} unidades en ese mismo periodo "
            "solo para no perder dinero (cubrir tus costos, sin ganar ni perder).\n"
            f"{linea_utilidad}\n\n"
            "🔍 *Nota:* Este cálculo es una guía general y simplificada (no incluye impuestos ni el efecto "
            "de un préstamo, aunque si tienes uno puedes sumar el pago que hagas en tu periodo a tus costos fijos). No "
            "sustituye la asesoría de un contador."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"

# --- Calculadora completa: incluye crédito, depreciación e impuestos ---
def calcular_pago_credito_primer_periodo(capital, tasa_periodo, plazo_total):
    """
    Dado el monto de un crédito, la tasa de interés YA CONVERTIDA a un periodo
    (por ejemplo mensual, quincenal o semanal: el mismo periodo que la
    persona eligió para analizar su negocio) y el número total de pagos en ese
    mismo periodo, calcula el interés y el pago a capital del PRIMER pago,
    usando el sistema de pagos fijos (igual que en la calculadora de Crédito).
    Solo se necesita el primer periodo porque es lo que usa la fórmula de
    precio. Usar calcular_plazo_y_tasa_periodo() para obtener tasa_periodo y
    plazo_total a partir de años + tasa anual + frecuencia elegida, para que
    el crédito quede en el MISMO periodo que el resto del análisis.
    Si no hay crédito (capital 0), regresa (0, 0).
    """
    capital = Decimal(str(capital))
    plazo_total = int(plazo_total)
    tasa_periodo = Decimal(str(tasa_periodo))

    if capital <= 0 or plazo_total <= 0:
        return Decimal("0"), Decimal("0")

    if tasa_periodo == 0:
        pago = capital / Decimal(plazo_total)
    else:
        pago = (capital * tasa_periodo) / (Decimal("1") - (Decimal("1") + tasa_periodo) ** Decimal(-plazo_total))

    interes = (capital * tasa_periodo).quantize(Decimal("0.01"))
    amortizacion_capital = (pago - interes).quantize(Decimal("0.01"))
    return interes, amortizacion_capital

def calcular_precio_sugerido_emprendedor_completo(
    unidades, costo_unitario, pct_variable, costos_fijos, depreciacion,
    interes, amortizacion_capital, tasa_impositiva, utilidad_deseada,
):
    """
    Versión completa de la calculadora de precio por costeo, que además de lo
    que ya considera la versión rápida (unidades, costo por unidad, costos
    fijos y utilidad deseada), incorpora costos variables en %, depreciación,
    el efecto de un crédito del negocio (intereses y pago a capital) y la
    tasa de impuestos sobre la utilidad. Cuando el pago a capital del crédito
    supera a la depreciación en el periodo, se ajusta el precio para que,
    después de impuestos, siga alcanzando la utilidad deseada (porque el pago
    a capital no es deducible de impuestos, a diferencia de la depreciación).
    """
    unidades = Decimal(str(unidades))
    costo_unitario = Decimal(str(costo_unitario))
    pct_variable_frac = Decimal(str(pct_variable)) / Decimal("100")
    costos_fijos = Decimal(str(costos_fijos))
    depreciacion = Decimal(str(depreciacion))
    interes = Decimal(str(interes))
    amortizacion_capital = Decimal(str(amortizacion_capital))
    tasa_impositiva_frac = Decimal(str(tasa_impositiva)) / Decimal("100")
    utilidad_deseada = Decimal(str(utilidad_deseada))

    selector = utilidad_deseada - depreciacion + amortizacion_capital
    if selector <= 0 or tasa_impositiva_frac >= 1:
        numerador_utilidad = utilidad_deseada
    else:
        numerador_utilidad = utilidad_deseada / (Decimal("1") - tasa_impositiva_frac)

    precio = (
        ((numerador_utilidad + costos_fijos + interes + depreciacion) / unidades) + costo_unitario
    ) / (Decimal("1") - pct_variable_frac)
    return precio.quantize(Decimal("0.01"))

def calcular_resultado_precio_emprendedor_completo(
    unidades, costo_unitario, pct_variable, costos_fijos, depreciacion,
    interes, tasa_impositiva, precio_elegido, frase_periodo="en tu periodo elegido",
):
    """
    A partir de un precio elegido, calcula el punto de equilibrio y la
    utilidad NETA (ya con impuestos) si se venden las unidades planeadas,
    mostrando el desglose completo de ingresos, costos y utilidad.
    frase_periodo es texto para mostrarle a la persona el periodo que eligió
    (por ejemplo "al mes" o "a la semana"), para que quede claro a qué
    periodo corresponden todos los montos, incluyendo el pago del crédito.
    """
    try:
        unidades = Decimal(str(unidades))
        costo_unitario = Decimal(str(costo_unitario))
        pct_variable_frac = Decimal(str(pct_variable)) / Decimal("100")
        costos_fijos = Decimal(str(costos_fijos))
        depreciacion = Decimal(str(depreciacion))
        interes = Decimal(str(interes))
        tasa_impositiva_frac = Decimal(str(tasa_impositiva)) / Decimal("100")
        precio_elegido = Decimal(str(precio_elegido))

        margen_unitario = precio_elegido * (Decimal("1") - pct_variable_frac) - costo_unitario
        punto_equilibrio = (
            (costos_fijos + interes + depreciacion) / margen_unitario
        ).to_integral_value(rounding=ROUND_CEILING)

        ingresos = precio_elegido * unidades
        costos_variables_totales = (costo_unitario * unidades) + (pct_variable_frac * ingresos)
        utilidad_antes_int_imp = ingresos - costos_variables_totales - costos_fijos - depreciacion
        utilidad_antes_impuestos = utilidad_antes_int_imp - interes
        impuestos = (
            (utilidad_antes_impuestos * tasa_impositiva_frac) if utilidad_antes_impuestos > 0 else Decimal("0")
        )
        utilidad_neta = (utilidad_antes_impuestos - impuestos).quantize(Decimal("0.01"))

        if utilidad_neta >= 0:
            linea_utilidad = f"✅ Utilidad neta estimada (ya con impuestos): ${utilidad_neta:,.2f}"
        else:
            linea_utilidad = f"⚠️ Pérdida neta estimada: ${abs(utilidad_neta):,.2f}"

        return (
            "📌 Resultado con el precio que elegiste:\n"
            f"💲 Precio por unidad: ${precio_elegido:,.2f}\n"
            f"💵 Ingresos totales {frase_periodo} ({unidades:,.0f} unidades): ${ingresos:,.2f}\n"
            f"🧮 Costos variables (costo por unidad + % sobre ventas): ${costos_variables_totales:,.2f}\n"
            f"📉 Costos fijos {frase_periodo}: ${costos_fijos:,.2f}\n"
            f"🏭 Depreciación {frase_periodo}: ${depreciacion:,.2f}\n"
            f"🏦 Intereses del crédito (primer pago): ${interes:,.2f}\n"
            f"🧾 Impuestos: ${impuestos.quantize(Decimal('0.01')):,.2f}\n\n"
            f"⚖️ Punto de equilibrio: necesitas vender {punto_equilibrio:,.0f} unidades {frase_periodo} solo "
            "para no perder dinero.\n"
            f"{linea_utilidad}\n\n"
            "🔍 *Nota:* Este cálculo es una guía general. No sustituye la asesoría de un contador, sobre "
            "todo para el manejo correcto de tus impuestos."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"

# --- Calculadora para negocios turísticos ---
mensaje_turismo_intro = (
    "🏖️ *Calculadora para negocios turísticos*\n\n"
    "En turismo (tours, hospedaje, experiencias, renta de equipo, etc.) el costeo funciona un poco "
    "distinto al de un negocio que vende productos:\n"
    "📌 Tienes una CAPACIDAD máxima (cupo del tour, cuartos, lugares en la van), y si no la llenas, ese "
    "espacio se pierde para siempre.\n"
    "📌 Muchos de tus costos (guía, transporte, permisos, renta del local) se pagan por SALIDA o por "
    "noche, sin importar cuántas personas vayan.\n"
    "📌 Si vendes a través de plataformas (Booking, Airbnb, Viator, TripAdvisor, agencias de viajes), "
    "suelen cobrarte una comisión, comúnmente entre 15% y 25% según la plataforma.\n"
    "📌 La demanda cambia entre temporada alta y baja, así que conviene calcular pensando en tu ocupación "
    "PROMEDIO del año, no solo en tus mejores días.\n\n"
    "Vamos a calcular cuánto te conviene cobrar por persona.\n\n"
    "1️⃣ ¿Cuál es la capacidad máxima de tu tour, cuarto o servicio? (ejemplo: si tu van o lancha lleva 12 "
    "personas, escribe 12)"
)

def calcular_precio_sugerido_turismo(capacidad, ocupacion_pct, costos_fijos, costo_variable, comision_pct, utilidad_deseada):
    capacidad = Decimal(str(capacidad))
    ocupacion_frac = Decimal(str(ocupacion_pct)) / Decimal("100")
    costos_fijos = Decimal(str(costos_fijos))
    costo_variable = Decimal(str(costo_variable))
    comision_frac = Decimal(str(comision_pct)) / Decimal("100")
    utilidad_deseada = Decimal(str(utilidad_deseada))

    # Se redondea a personas enteras (no se puede llevar, por ejemplo, 0.2
    # personas en la práctica), con un mínimo de 1 para no dividir entre
    # cero si la capacidad u ocupación son muy bajas.
    personas_esperadas = (capacidad * ocupacion_frac).to_integral_value(rounding=ROUND_HALF_UP)
    if personas_esperadas < 1:
        personas_esperadas = Decimal("1")
    precio_base = costo_variable + (costos_fijos + utilidad_deseada) / personas_esperadas
    precio_sugerido = (precio_base / (Decimal("1") - comision_frac)).quantize(Decimal("0.01"))
    return precio_sugerido, personas_esperadas

def calcular_resultado_turismo(capacidad, costos_fijos, costo_variable, comision_pct, personas_esperadas, precio_elegido):
    """
    Devuelve (texto_resultado, es_viable, margen_persona). es_viable indica
    si a ese precio se puede seguir con el punto de equilibrio MENSUAL
    (opción aparte): si el margen por persona ya es negativo o cero, no
    tiene caso preguntar por costos mensuales del negocio.
    """
    try:
        capacidad = Decimal(str(capacidad))
        costos_fijos = Decimal(str(costos_fijos))
        costo_variable = Decimal(str(costo_variable))
        comision_frac = Decimal(str(comision_pct)) / Decimal("100")
        precio_elegido = Decimal(str(precio_elegido))

        ingreso_neto_persona = precio_elegido * (Decimal("1") - comision_frac)
        margen_persona = ingreso_neto_persona - costo_variable
        if margen_persona <= 0:
            return (
                f"⚠️ A ${precio_elegido:,.2f} por persona, después de la comisión no alcanzas ni a cubrir tu "
                f"costo variable por persona (${costo_variable:,.2f}), así que entre más gente lleves, más "
                "perderías. Prueba con un precio mayor.",
                False,
                margen_persona,
            )

        punto_equilibrio_personas = (costos_fijos / margen_persona).to_integral_value(rounding=ROUND_CEILING)
        punto_equilibrio_ocupacion_pct = (punto_equilibrio_personas / capacidad * Decimal("100")).quantize(Decimal("0.1"))
        utilidad_estimada = (margen_persona * personas_esperadas - costos_fijos).quantize(Decimal("0.01"))

        if utilidad_estimada >= 0:
            linea_utilidad = (
                f"✅ Con tu ocupación promedio esperada ({personas_esperadas:,.0f} personas por salida), "
                f"tendrías una utilidad estimada de ${utilidad_estimada:,.2f} por salida."
            )
        else:
            linea_utilidad = (
                f"⚠️ Con tu ocupación promedio esperada ({personas_esperadas:,.0f} personas por salida), "
                f"tendrías una pérdida estimada de ${abs(utilidad_estimada):,.2f} por salida."
            )

        linea_comision = f"🏷️ Comisión de plataforma: {comision_pct}%\n" if comision_frac > 0 else ""

        return (
            (
                "📌 Resultado con el precio que elegiste:\n"
                f"💲 Precio por persona: ${precio_elegido:,.2f}\n"
                f"🧮 Costo variable por persona: ${costo_variable:,.2f}\n"
                f"📉 Costos fijos por salida: ${costos_fijos:,.2f}\n"
                f"{linea_comision}\n"
                f"⚖️ Punto de equilibrio: necesitas {punto_equilibrio_personas:,.0f} personas por salida "
                f"({punto_equilibrio_ocupacion_pct}% de tu capacidad de {capacidad:,.0f}) solo para no perder "
                "dinero.\n"
                f"{linea_utilidad}\n\n"
                "💡 En temporada baja, en vez de bajar mucho el precio, considera armar paquetes o promociones "
                "que junten más personas por salida: buena parte de tus costos son fijos por salida, no por "
                "persona.\n"
                "💡 Define una política de cancelación clara: en turismo, un lugar cancelado a última hora casi "
                "siempre se pierde.\n\n"
                "🔍 *Nota:* Este cálculo es una guía general y simplificada. No sustituye la asesoría de un "
                "contador."
            ),
            True,
            margen_persona,
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}", False, None

def calcular_punto_equilibrio_mensual_turismo(
    margen_persona, personas_esperadas, capacidad, costos_fijos_por_salida, tours_mensuales,
    costos_fijos_mensuales_negocio,
):
    """
    Extiende el cálculo por salida a una vista mensual, dando por hecho que
    vas a dar `tours_mensuales` tours ese mes (ese número ya lo dio la
    persona, no se recalcula aquí). Con ese número fijo de tours, se
    reparte el total de personas del mes que hacen falta para cubrir TODOS
    los costos fijos del mes: los costos fijos por salida (que se pagan una
    vez por cada tour que realmente se dé, sin importar cuántas personas
    vayan) multiplicados por esos tours, más los costos fijos generales del
    negocio.

    OJO: la primera versión de este cálculo sacaba cuántos tours COMPLETOS
    (a la ocupación esperada) hacían falta para cubrir solo los costos
    fijos del negocio, y multiplicaba eso por la ocupación esperada. Eso
    sobreestimaba mucho el punto de equilibrio, porque de por sí un tour
    completo a la ocupación esperada casi siempre deja bastante más
    ganancia de la que hace falta para cubrir el resto de los costos fijos
    del mes: exigía llenar tours enteros en vez de repartir el mínimo de
    personas necesario entre los tours que de todas formas se van a dar.
    """
    margen_persona = Decimal(str(margen_persona))
    personas_esperadas = Decimal(str(personas_esperadas))
    capacidad = Decimal(str(capacidad))
    costos_fijos_por_salida = Decimal(str(costos_fijos_por_salida))
    tours_mensuales = Decimal(str(tours_mensuales))
    costos_fijos_mensuales_negocio = Decimal(str(costos_fijos_mensuales_negocio))

    # Ganancia que deja cada salida a tu ocupación esperada, ya cubriendo
    # su propio costo fijo por salida, antes de los costos fijos
    # mensuales del negocio en general. Es solo informativo (para que veas
    # qué tan rentable es tu tour "típico"), el punto de equilibrio de
    # abajo NO se calcula a partir de este número.
    margen_por_tour_esperado = margen_persona * personas_esperadas - costos_fijos_por_salida

    # Todos los costos fijos del mes: los que se pagan por cada tour que
    # realmente se dé (independientemente de cuánta gente vaya) más los
    # costos fijos generales del negocio.
    costos_fijos_totales_mes = (costos_fijos_por_salida * tours_mensuales) + costos_fijos_mensuales_negocio
    ingresos_mensuales_esperados = (margen_por_tour_esperado * tours_mensuales).quantize(Decimal("0.01"))
    utilidad_mensual = (ingresos_mensuales_esperados - costos_fijos_mensuales_negocio).quantize(Decimal("0.01"))

    if utilidad_mensual >= 0:
        linea_utilidad_mensual = (
            f"✅ Con {tours_mensuales:,.0f} tours al mes a tu ocupación esperada, tendrías una utilidad "
            f"estimada de ${utilidad_mensual:,.2f} al mes (después de cubrir tus costos fijos mensuales del "
            "negocio)."
        )
    else:
        linea_utilidad_mensual = (
            f"⚠️ Con {tours_mensuales:,.0f} tours al mes a tu ocupación esperada, tendrías una pérdida "
            f"estimada de ${abs(utilidad_mensual):,.2f} al mes (después de tus costos fijos mensuales del "
            "negocio)."
        )

    capacidad_maxima_mes = capacidad * tours_mensuales
    personas_necesarias_mes = (costos_fijos_totales_mes / margen_persona).to_integral_value(rounding=ROUND_CEILING)

    if personas_necesarias_mes > capacidad_maxima_mes:
        linea_equilibrio_mensual = (
            f"🚨 Ni llenando tus {tours_mensuales:,.0f} tours al 100% de su capacidad "
            f"({capacidad_maxima_mes:,.0f} personas en el mes) alcanzarías a cubrir tus "
            f"${costos_fijos_totales_mes:,.2f} de costos fijos totales del mes (costos por salida de tus "
            f"tours + costos fijos generales del negocio). Con estos números necesitas dar más tours al "
            "mes, subir el precio, o reducir tus costos fijos.\n"
        )
    else:
        promedio_necesario_por_tour = (personas_necesarias_mes / tours_mensuales).quantize(Decimal("0.1"))
        ocupacion_necesaria_pct = (promedio_necesario_por_tour / capacidad * Decimal("100")).quantize(Decimal("0.1"))
        linea_equilibrio_mensual = (
            f"⚖️ Punto de equilibrio mensual: dando tus {tours_mensuales:,.0f} tours al mes, necesitas un "
            f"total de {personas_necesarias_mes:,.0f} personas en todo el mes (un promedio de "
            f"{promedio_necesario_por_tour} personas por tour, {ocupacion_necesaria_pct}% de tu capacidad de "
            f"{capacidad:,.0f}) solo para cubrir tus ${costos_fijos_totales_mes:,.2f} de costos fijos totales "
            "del mes (costos por salida de tus tours + costos fijos generales del negocio).\n"
        )

    def _fmt_dinero(monto):
        # Igual que en otras calculadoras: si el monto es negativo, el
        # signo va antes del $ (-$500.00) en vez de después ($-500.00).
        return f"-${abs(float(monto)):,.2f}" if monto < 0 else f"${float(monto):,.2f}"

    return (
        "\n\n________________________________________\n"
        "📆 *Vista mensual de tu negocio*\n"
        f"💰 Ganancia por tour a tu ocupación esperada (ya cubriendo su costo fijo por salida): "
        f"{_fmt_dinero(margen_por_tour_esperado)}\n"
        f"💵 Ganancia estimada de tus tours antes de costos fijos mensuales del negocio: "
        f"{_fmt_dinero(ingresos_mensuales_esperados)}\n"
        f"{linea_equilibrio_mensual}"
        f"{linea_utilidad_mensual}\n\n"
        "🔍 *Nota:* Este cálculo asume que el número de tours y la ocupación promedio se mantienen estables "
        "mes con mes; en la práctica, revísalo cada temporada."
    )

mensaje_emprendedor_tips = (
    "💡 *Tips financieros para tu negocio*\n\n"
    "Aquí van algunas ideas que le ayudan a cualquier negocio pequeño a mantenerse más sano financieramente:\n"
    "________________________________________\n"
    "✅ 1. Separa el dinero de tu negocio del dinero personal\n"
    "📌 Aunque tu negocio sea informal, procura tener una cuenta o al menos un espacio distinto para ese "
    "dinero.\n"
    "💡 Así vas a saber de verdad si tu negocio gana dinero o no, sin mezclarlo con tus gastos personales.\n"
    "________________________________________\n"
    "✅ 2. Lleva un registro simple de lo que entra y lo que sale\n"
    "📌 No necesitas un sistema complicado, una libreta o una hoja de cálculo básica es un buen comienzo.\n"
    "💡 Sin ese registro, es muy fácil pensar que tu negocio va mejor (o peor) de lo que realmente va.\n"
    "________________________________________\n"
    "✅ 3. Construye un colchón de flujo de efectivo para tu negocio\n"
    "📌 Además de tu fondo de emergencia personal, tu negocio también necesita uno: para meses lentos, "
    "imprevistos, o para aprovechar oportunidades de comprar más inventario.\n"
    "💡 Un negocio se puede quedar sin dinero en caja aunque esté ganando en papel.\n"
    "________________________________________\n"
    "✅ 4. No confundas utilidad con tener dinero disponible\n"
    "📌 Puedes tener ganancias en papel y aun así no tener efectivo en la mano, por ejemplo si vendes a "
    "crédito, tienes mucho inventario guardado, o estás pagando un préstamo.\n"
    "💡 Revisa ambas cosas por separado: cuánto ganas y cuánto dinero tienes realmente disponible.\n\n"
)

# =========================================
# Impuestos y cómo afectan tus finanzas
# =========================================
# Tarifa mensual de ISR (Art. 96 LISR) vigente en 2026: (límite inferior, límite
# superior o None si no tiene, cuota fija, % sobre excedente del límite inferior).
TABLA_ISR_MENSUAL = [
    (Decimal("0.01"), Decimal("844.59"), Decimal("0.00"), Decimal("1.92")),
    (Decimal("844.60"), Decimal("7168.51"), Decimal("16.22"), Decimal("6.40")),
    (Decimal("7168.52"), Decimal("12598.02"), Decimal("420.95"), Decimal("10.88")),
    (Decimal("12598.03"), Decimal("14644.64"), Decimal("1011.68"), Decimal("16.00")),
    (Decimal("14644.65"), Decimal("17533.64"), Decimal("1339.14"), Decimal("17.92")),
    (Decimal("17533.65"), Decimal("35362.83"), Decimal("1856.84"), Decimal("21.36")),
    (Decimal("35362.84"), Decimal("55736.68"), Decimal("5665.16"), Decimal("23.52")),
    (Decimal("55736.69"), Decimal("106410.50"), Decimal("10457.09"), Decimal("30.00")),
    (Decimal("106410.51"), Decimal("141880.66"), Decimal("25659.23"), Decimal("32.00")),
    (Decimal("141880.67"), Decimal("425641.99"), Decimal("37009.69"), Decimal("34.00")),
    (Decimal("425642.00"), None, Decimal("133488.54"), Decimal("35.00")),
]

def calcular_isr_mensual(ingreso_mensual):
    """
    Calcula el ISR mensual sobre un ingreso (sueldo o utilidad de negocio),
    usando la tarifa progresiva del Art. 96 de la LISR: se paga la cuota fija
    del rango donde cae el ingreso, más el % de ese rango solo sobre lo que
    excede el límite inferior (no sobre todo el ingreso). Regresa el ISR y la
    tasa marginal (el % del rango en el que cayó el ingreso).
    """
    ingreso = Decimal(str(ingreso_mensual))
    if ingreso <= 0:
        return Decimal("0.00"), Decimal("0")
    for limite_inferior, limite_superior, cuota_fija, tasa_pct in TABLA_ISR_MENSUAL:
        if limite_superior is None or ingreso <= limite_superior:
            excedente = ingreso - limite_inferior
            isr = cuota_fija + (excedente * tasa_pct / Decimal("100"))
            return isr.quantize(Decimal("0.01")), tasa_pct
    ultimo = TABLA_ISR_MENSUAL[-1]
    excedente = ingreso - ultimo[0]
    isr = ultimo[2] + (excedente * ultimo[3] / Decimal("100"))
    return isr.quantize(Decimal("0.01")), ultimo[3]

# Tabla RESICO mensual (Art. 113-E LISR) vigente en 2026: a diferencia del ISR
# general, aquí la tasa se aplica directo sobre TODO el ingreso, sin cuota fija.
TABLA_RESICO_MENSUAL = [
    (Decimal("0.01"), Decimal("25000.00"), Decimal("1.00")),
    (Decimal("25000.01"), Decimal("50000.00"), Decimal("1.10")),
    (Decimal("50000.01"), Decimal("83333.33"), Decimal("1.50")),
    (Decimal("83333.34"), Decimal("208333.33"), Decimal("2.00")),
    (Decimal("208333.34"), None, Decimal("2.50")),
]

def calcular_isr_resico_mensual(ingreso_mensual):
    ingreso = Decimal(str(ingreso_mensual))
    if ingreso <= 0:
        return Decimal("0.00"), Decimal("0")
    for limite_inferior, limite_superior, tasa_pct in TABLA_RESICO_MENSUAL:
        if limite_superior is None or ingreso <= limite_superior:
            isr = ingreso * tasa_pct / Decimal("100")
            return isr.quantize(Decimal("0.01")), tasa_pct
    ultimo = TABLA_RESICO_MENSUAL[-1]
    isr = ingreso * ultimo[2] / Decimal("100")
    return isr.quantize(Decimal("0.01")), ultimo[2]

mensaje_submenu_impuestos = (
    "🧾 *Impuestos y cómo afectan tus finanzas*\n\n"
    "Entender tus impuestos te puede ayudar a pagar solo lo justo, y hasta a recuperar dinero. ¿Qué te "
    "gustaría ver?\n\n"
    "1️⃣ ¿Por qué me descuentan tanto de mi sueldo? Calcula tu tasa de ISR\n"
    "2️⃣ ¿Puedo recibir una devolución de impuestos?\n"
    "3️⃣ Tengo o quiero un negocio: ¿qué es RESICO y por qué puede convenirme?\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_impuestos_devolucion = (
    "💸 *¿Puedo recibir una devolución de impuestos?*\n\n"
    "Sí. Cada año (normalmente en abril) puedes presentar tu Declaración Anual ante el SAT. Si a lo largo "
    "del año te retuvieron más ISR del que en realidad debías pagar, o si tienes gastos que puedes deducir, "
    "el SAT te regresa la diferencia.\n"
    "________________________________________\n"
    "🧾 *Gastos que puedes deducir* (deducciones personales), si los pagaste con transferencia, tarjeta o "
    "cheque (NO en efectivo) y guardaste tu factura (CFDI):\n"
    "🩺 Honorarios médicos, dentales, de psicología y nutrición\n"
    "🏥 Gastos hospitalarios y análisis clínicos\n"
    "👓 Lentes ópticos graduados\n"
    "🛡️ Primas de seguros de gastos médicos\n"
    "🎓 Colegiaturas, de preescolar a bachillerato (cada nivel tiene su propio tope)\n"
    "🚌 Transporte escolar, solo si tu escuela lo hace obligatorio\n"
    "🏦 Aportaciones voluntarias a tu Afore o plan de retiro\n"
    "🏠 Intereses reales de tu crédito hipotecario\n"
    "🕊️ Gastos funerarios de tu cónyuge, padres, hijos o abuelos\n"
    "🤝 Donativos a instituciones autorizadas por el SAT\n"
    "________________________________________\n"
    "⚖️ Todas tus deducciones personales juntas tienen un tope: lo que sea MENOR entre 5 UMAs anuales "
    "(alrededor de $206,418 en 2026) o el 15% de tus ingresos anuales.\n\n"
    "🔍 *Nota:* Estos montos y topes los actualiza el SAT cada año; confirma las cifras vigentes en el "
    "portal del SAT o con un contador antes de declarar."
)

mensaje_impuestos_resico_intro = (
    "🚀 Si tienes o estás por abrir un negocio, seguro te preocupa cuánto vas a pagar de impuestos. A "
    "muchas personas les da miedo darse de alta ante el SAT, pero existe un régimen pensado justo para "
    "negocios que empiezan: el *RESICO* (Régimen Simplificado de Confianza).\n\n"
    "🔑 En RESICO pagas una tasa muy baja (de 1% a 2.5%) sobre TODO lo que facturas, sin restar tus "
    "gastos. En el régimen general pagas una tasa más alta, pero solo sobre tu utilidad (lo que te queda "
    "después de tus gastos comprobables).\n\n"
    "Para tributar en RESICO tus ingresos del año no deben superar $3,500,000, y aplica para personas "
    "físicas con actividad empresarial, honorarios (servicios profesionales) o arrendamiento (hay algunas "
    "excepciones, como ser socio/a de una empresa; el SAT tiene el detalle completo).\n\n"
    "Vamos a comparar los dos con tus números.\n\n"
    "1️⃣ ¿Cuánto facturas o esperas facturar al mes en tu negocio? (ejemplo: 20000)"
)

def calcular_comparacion_resico(ingreso_mensual, gastos_mensuales):
    ingreso = Decimal(str(ingreso_mensual))
    gastos = Decimal(str(gastos_mensuales))

    resico_isr, resico_tasa = calcular_isr_resico_mensual(ingreso)
    utilidad = max(ingreso - gastos, Decimal("0"))
    general_isr, general_tasa_marginal = calcular_isr_mensual(utilidad)

    if resico_isr <= general_isr:
        veredicto = "✅ Con tus números, RESICO te sale más barato."
    else:
        veredicto = "✅ Con tus números, el régimen general te sale más barato (por tus gastos comprobables)."

    alerta_limite = ""
    if ingreso * Decimal("12") > Decimal("3500000"):
        alerta_limite = (
            "\n\n⚠️ Con ese ritmo de facturación, tus ingresos del año podrían superar los $3,500,000, que "
            "es el límite para seguir en RESICO. Lo que importa es tu acumulado anual, así que ve llevando "
            "la cuenta."
        )

    return (
        f"📊 Con ${ingreso:,.2f} facturados al mes:\n\n"
        f"🟢 En RESICO pagarías: ${resico_isr:,.2f} de ISR (tasa de {resico_tasa}% sobre todo lo que "
        "facturas)\n"
        f"🔵 En el régimen general, sobre tu utilidad estimada de ${utilidad:,.2f} (tus ingresos menos los "
        f"gastos que me diste), pagarías: ${general_isr:,.2f} de ISR\n\n"
        f"{veredicto}{alerta_limite}\n\n"
        "🔍 *Nota:* Esta comparación es una referencia simplificada; no incluye otras contribuciones (como "
        "el IVA) ni el costo de llevar la contabilidad de cada régimen. No sustituye la asesoría de un "
        "contador."
    )

# =========================================
# Menú principal
# =========================================
saludo_inicial = (
    "👋 Hola 😊, soy tu asistente virtual de Educación Financiera para el Mundo, un proyecto de la "
    "Facultad de Ciencias Administrativas de la Universidad Autónoma de Baja California (UABC) y "
    "estoy aquí para ayudarte a comprender mejor el mundo de las finanzas.\n\n"
    "Escríbeme el número o el nombre de alguna de estas opciones para empezar:\n"
    "1️⃣ Ahorro\n"
    "2️⃣ Crédito\n"
    "3️⃣ Inversión\n"
    "4️⃣ Jubilación\n"
    "5️⃣ Herramientas para el emprendedor\n"
    "6️⃣ Género y finanzas\n"
    "7️⃣ Impuestos y cómo afectan tus finanzas\n"
    "8️⃣ Protege tus finanzas: seguros y fraudes\n"
    "9️⃣ Evalúa tu salud financiera\n"
    "🔟 Glosario de términos financieros\n\n"
    "✨ Y si quieres conocer al equipo:\n"
    "1️⃣1️⃣ ¿Quiénes hicimos este bot?\n\n"
    "No te preocupes si no conoces todos estos términos, yo te voy guiando paso a paso 😊\n\n"
    "🔒 Este bot nunca te va a pedir contraseñas, NIP, CVV de tu tarjeta ni códigos de verificación. "
    "Si alguien más te los pide haciéndose pasar por este bot, no se los compartas.\n\n"
    "📊 Para poder mejorar este bot, registramos de forma anónima qué opciones se usan (nunca tu número "
    "ni el contenido de tus mensajes)."
)

# Si el servicio estuvo dormido por inactividad (plan gratuito de Render) y
# despierta, se pierde el hilo de cualquier conversación en curso. Si alguien
# sin conversación activa nos escribe algo que parece la respuesta a una
# pregunta (por ejemplo, solo un número) en vez de un saludo o un comando,
# probablemente se quedó a la mitad de algo; en ese caso le explicamos qué
# pasó en vez de mostrarle la bienvenida como si nada.
mensaje_sesion_reiniciada = (
    "🙏 Antes de seguir: parece que pasó un rato desde tu último mensaje y tuve que reiniciar nuestra "
    "conversación (así funciona el servicio donde vivo: se \"duerme\" tras un rato sin uso). Disculpa las "
    "molestias, si estabas a la mitad de algo vamos a tener que empezar de nuevo.\n\n"
) + saludo_inicial

def _parece_respuesta_de_conversacion_perdida(texto_limpio):
    if not texto_limpio or texto_limpio in [
        "hola", "menu", "menú", "buenas", "buenos días", "buenos dias",
        "buenas tardes", "buenas noches", "hi", "hello",
    ]:
        return False
    try:
        Decimal(texto_limpio.replace(",", "").replace("$", "").replace("%", ""))
        return True
    except Exception:
        return False

mensaje_submenu_ahorro = (
    "💰 *Ahorro*\n\n"
    "1️⃣ ¿Cuánto debo apartar para lograr mi meta de ahorro?\n"
    "2️⃣ Consejos para ahorrar sin sufrir en el intento\n"
    "3️⃣ ¿Dónde puedo comparar cuentas de ahorro entre bancos?\n"
    "4️⃣ Calculadora de presupuesto (regla 50/30/20)\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_intro_presupuesto = (
    "📊 *Calculadora de presupuesto: regla 50/30/20*\n\n"
    "Es una guía sencilla para organizar tu ingreso mensual en 3 partes:\n"
    "🏠 50% a tus gastos necesarios (renta, comida, transporte, servicios)\n"
    "🎉 30% a tus gustos (lo que quieras: salidas, streaming, ropa, etc.)\n"
    "💰 20% a ahorro o pago de deudas\n\n"
    "No tiene que ser exacta, pero te da un punto de partida. Vamos a calcular tus montos:\n\n"
    "1️⃣ ¿Cuál es tu ingreso mensual neto? Es decir, lo que realmente recibes después de "
    "impuestos: lo que te depositan o te dan en efectivo. (ejemplo: 12000)"
)

mensaje_submenu_credito = (
    "💳 *Crédito*\n\n"
    "1️⃣ Simular un crédito\n"
    "2️⃣ Ahorro con pagos extra a un crédito\n"
    "3️⃣ Costo real de comprar a plazos en tiendas\n"
    "4️⃣ Pago mínimo de tu tarjeta: cómo se calcula y qué te cuesta\n"
    "5️⃣ ¿Cuánto me pueden prestar?\n"
    "6️⃣ Consejos para pagar sin ahogarte\n"
    "7️⃣ Identificar un crédito caro\n"
    "8️⃣ Errores comunes al pedir crédito\n"
    "9️⃣ Entender el Buró de Crédito\n"
    "🔟 Tus derechos frente al cobro de deudas\n"
    "1️⃣1️⃣ Plan para pagar varias deudas (bola de nieve o avalancha)\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_intro_pago_minimo = (
    "💳 *Pago mínimo de tu tarjeta: cómo se calcula*\n\n"
    "En México, Banxico obliga a los bancos a cobrar como mínimo el MAYOR entre estos dos "
    "montos (más los intereses del periodo y su IVA):\n"
    "1️⃣ 1.5% de tu saldo deudor (lo que debes)\n"
    "2️⃣ 1.25% de tu límite de crédito\n\n"
    "Los bancos pueden pedirte un mínimo más alto que eso, pero nunca uno menor.\n\n"
    "Vamos a calcular el tuyo y ver qué pasaría si solo pagaras el mínimo cada mes, sin volver "
    "a usar la tarjeta. Dime:\n\n"
    "1️⃣ ¿Cuál es el saldo actual (deuda) de tu tarjeta? (ejemplo: 18000)"
)

mensaje_intro_plan_deudas = (
    "🎯 *Plan para pagar varias deudas*\n\n"
    "Si tienes más de una deuda (tarjetas, préstamos, etc.), el orden en que las pagas sí "
    "importa. Te muestro dos estrategias muy usadas:\n\n"
    "❄️ *Bola de nieve*: primero liquidas la deuda con el saldo MÁS PEQUEÑO (sin importar su "
    "tasa), y sigues con la siguiente. Psicológicamente ayuda mucho: vas viendo deudas "
    "desaparecer rápido y eso motiva a seguir.\n\n"
    "🏔️ *Avalancha*: primero liquidas la deuda con la tasa de interés MÁS ALTA. Matemáticamente "
    "es la que menos intereses totales te hace pagar.\n\n"
    "En ambas, sigues pagando el mínimo de todas tus demás deudas mientras tanto, y cuando "
    "terminas de pagar una, ese dinero se suma al abono de la siguiente (por eso \"bola de "
    "nieve\": va creciendo).\n\n"
    "Vamos a comparar las dos con tus datos. ¿Cuántas deudas quieres incluir? (un número del 2 "
    "al 6)"
)

mensaje_submenu_inversion = (
    "📈 *Inversión*\n\n"
    "1️⃣ ¿Cuánto puede crecer mi dinero si invierto?\n"
    "2️⃣ Conceptos básicos antes de invertir\n"
    "3️⃣ CETES y Cetesdirecto: invertir con bajo riesgo\n"
    "4️⃣ Cómo identificar fraudes de inversión\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_submenu_jubilacion = (
    "🌅 *Jubilación*\n\n"
    "1️⃣ Calculadoras de jubilación\n"
    "2️⃣ ¿Qué es una Afore y cómo saber en cuál estoy?\n"
    "3️⃣ ¿Cómo se calcula mi pensión? Ley 73 vs. Ley 97\n"
    "4️⃣ Aportaciones voluntarias: cómo aumentar tu ahorro para el retiro\n"
    "5️⃣ ¿Qué pasa si cambio de trabajo o dejo de cotizar?\n"
    "6️⃣ No he trabajado de forma formal, ¿aún así puedo ahorrar para mi retiro?\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_calculadoras_jubilacion = (
    "🧮 *Calculadoras de jubilación*\n\n"
    "Estas calculadoras estiman, a partir de ciertos supuestos, cuál podría ser tu saldo y tu pensión al "
    "llegar al retiro, con la misma metodología (o una versión simplificada de ella) que usan las "
    "calculadoras oficiales de CONSAR y de las Afores. Elige la que te corresponde:\n\n"
    "1️⃣ Trabajadores que cotizan al IMSS (Régimen de Ley 97)\n"
    "2️⃣ Trabajadores que cotizaban al IMSS antes de julio de 1997 (Régimen de Ley 73)\n"
    "3️⃣ Trabajadores que cotizan al ISSSTE (Régimen de cuentas individuales)\n"
    "4️⃣ Trabajadores independientes\n"
    "5️⃣ Tutorial para el uso de las calculadoras\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_jubilacion_tutorial = (
    "📖 *Tutorial para el uso de las calculadoras*\n\n"
    "Estas calculadoras te piden datos personales (tu saldo actual, tu edad, tu sueldo, etc.) para hacer "
    "una estimación, no para guardarlos: no compartas aquí contraseñas, NIP ni códigos de verificación.\n"
    "________________________________________\n"
    "📌 Tu *saldo actual en tu cuenta individual* lo puedes consultar gratis en la app de tu Afore, en "
    "Aforeweb, o en la app del SAR (CONSAR).\n"
    "📌 Si cotizas al IMSS bajo Ley 97, tu *salario base de cotización* aparece en tu recibo de nómina o en "
    "tu constancia de semanas cotizadas del IMSS; si no lo tienes a la mano, puedes usar tu sueldo mensual "
    "aproximado (el bot calcula tu rango salarial automáticamente a partir de este dato).\n"
    "📌 Si estás en Ley 73 (cotizaste al IMSS antes de julio de 1997), necesitas tu *salario promedio de "
    "las últimas 250 semanas* (casi 5 años) y tus *semanas cotizadas totales*; ambos los puedes consultar "
    "en la app del IMSS Digital.\n"
    "📌 Si cotizas al ISSSTE, tu *sueldo básico mensual* y tu *Bono de Pensión ISSSTE* (si tienes uno) "
    "también aparecen en tu recibo de nómina o en tu estado de cuenta de PENSIONISSSTE.\n"
    "📌 Los resultados son estimaciones para fines ilustrativos, no un cálculo oficial ni vinculante: para "
    "un número exacto, usa siempre la calculadora oficial de CONSAR "
    "(https://www.consar.gob.mx/gobmx/aplicativo/calculadora/Calculadoras/) o el trámite directo con tu "
    "institución.\n"
    "________________________________________\n"
) + "\n" + mensaje_calculadoras_jubilacion

# =========================================
# Herramientas para el emprendedor
# =========================================
mensaje_submenu_emprendedor = (
    "🧰 *Herramientas para el emprendedor*\n\n"
    "1️⃣ ¿A cuánto debo vender? (calculadora rápida)\n"
    "2️⃣ ¿A cuánto debo vender? (calculadora completa, con crédito, depreciación e impuestos)\n"
    "3️⃣ Calculadora para negocios turísticos (tours, hospedaje, experiencias)\n"
    "4️⃣ Tips financieros para tu negocio\n\n"
    "Escribe el número, o *menú* para regresar."
)

# =========================================
# Género y finanzas
# =========================================
mensaje_submenu_genero = (
    "♀️♂️ *Género y finanzas*\n\n"
    "1️⃣ La brecha de género en el ahorro para el retiro\n"
    "2️⃣ ¿Qué es la violencia económica y patrimonial?\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_genero_brecha_retiro = (
    "♀️ *La brecha de género en el ahorro para el retiro*\n\n"
    "En México, las mujeres suelen terminar con menos dinero ahorrado para su retiro que los hombres, y no "
    "es casualidad: hay razones estructurales detrás.\n"
    "________________________________________\n"
    "📊 Según CONSAR (2022), por cada 100 pesos de pensión que recibe un hombre, una mujer recibe "
    "aproximadamente 70.6 pesos.\n"
    "📊 En promedio, las mujeres tienen unos 24,000 pesos menos ahorrados en su cuenta Afore que los hombres "
    "(CONSAR, 2023).\n"
    "________________________________________\n"
    "¿Por qué pasa esto?\n"
    "📌 Interrupciones laborales por cuidados: las mujeres realizan el 74% del trabajo doméstico y de "
    "cuidados no remunerado en México (CONSAR, 2022), lo que muchas veces significa menos años cotizando.\n"
    "📌 Brecha salarial: según INEGI (2024), por cada 100 pesos que gana un hombre, una mujer gana en "
    "promedio 66; incluso comparando el mismo puesto de trabajo, la diferencia ronda el 15%.\n"
    "📌 Mayor esperanza de vida: las mujeres viven en promedio 2.4 años más después de los 65 (CONSAR, "
    "2022), así que su ahorro necesita alcanzar para más tiempo.\n"
    "________________________________________\n"
    "💡 Si te identificas con esto, dentro de *Jubilación* tienes herramientas que te pueden ayudar: la "
    "calculadora de meta de ahorro, cómo saber en qué Afore estás, cómo hacer aportaciones voluntarias, y "
    "opciones si no has trabajado de forma formal. Empezar temprano, aunque sea con poco, hace una "
    "diferencia real.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_genero

mensaje_genero_violencia_economica = (
    "⚖️ *¿Qué es la violencia económica y patrimonial?*\n\n"
    "En México, controlar el dinero de otra persona o dañar su patrimonio está reconocido legalmente como "
    "una forma de violencia (Ley General de Acceso de las Mujeres a una Vida Libre de Violencia, Artículo "
    "6).\n"
    "________________________________________\n"
    "📌 *Violencia patrimonial*: cuando alguien te quita, destruye, esconde o retiene tus objetos, "
    "documentos personales, bienes o recursos económicos que necesitas para vivir.\n"
    "📌 *Violencia económica*: cuando alguien controla o limita tu acceso a tu propio dinero, por ejemplo "
    "impidiéndote trabajar o manejar tus ingresos, o cuando te pagan menos que a otra persona por el mismo "
    "trabajo.\n"
    "________________________________________\n"
    "🚩 Algunas señales: que alguien te prohíba trabajar o estudiar, te quite tu sueldo o tarjetas, te pida "
    "cuentas de cada peso que gastas, te esconda información sobre las finanzas del hogar, o dañe tus bienes "
    "a propósito.\n"
    "________________________________________\n"
    "📢 Si estás viviendo una situación de violencia, llama al 911 en caso de emergencia. Para denunciar o "
    "pedir orientación, puedes acudir al Ministerio Público, a la Fiscalía, o al Instituto de las Mujeres de "
    "tu estado.\n"
    "________________________________________\n"
    "💡 Reconocer esto es el primer paso. Tener información y claridad sobre tus propias finanzas, como la "
    "que este bot te ofrece, también es una herramienta de autonomía.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_genero

# =========================================
# Evalúa tu salud financiera
# =========================================
# Basado en el "Semáforo de Salud Financiera" de la UABC: cada dimensión se
# evalúa pregunta por pregunta (escala 1-5) y el puntaje final se ubica en
# un rango 🔴/🟡/🟢, con una recomendación conectada al resto del bot.
mensaje_submenu_salud = (
    "🚦 *Evalúa tu salud financiera*\n\n"
    "Vamos a ver qué tan saludables están tus finanzas en 4 dimensiones:\n"
    "________________________________________\n"
    "🛡️ *Resiliencia*: tu capacidad para enfrentar imprevistos y emergencias económicas sin que se desestabilicen tus finanzas.\n"
    "🕊️ *Libertad*: qué tan libre te sientes de disfrutar tu dinero y alcanzar tus metas personales sin que la preocupación financiera te limite.\n"
    "🔐 *Seguridad*: qué tan protegido/a estás financieramente: tus ahorros, deudas, historial crediticio y seguros.\n"
    "🎯 *Control*: qué tanto le das seguimiento y manejas de forma consciente tus ingresos, gastos y decisiones financieras.\n"
    "________________________________________\n"
    "¿Quieres evaluar alguna? Te decimos cómo andas con un semáforo (🔴🟡🟢) y te ofrecemos contenido de este "
    "bot para seguir mejorando tu salud financiera.\n\n"
    "1️⃣ Resiliencia (5 preguntas)\n"
    "2️⃣ Libertad (5 preguntas)\n"
    "3️⃣ Seguridad (7 preguntas)\n"
    "4️⃣ Control (8 preguntas)\n"
    "5️⃣ Las 4 dimensiones completas (25 preguntas)\n\n"
    "Escribe el número, o *menú* para regresar."
)

# Versión corta del submenú: pie tras el resultado de una dimensión, sin
# repetir toda la explicación de arriba.
mensaje_salud_cierre = (
    "¿Quieres evaluar otra dimensión?\n"
    "1️⃣ Resiliencia\n"
    "2️⃣ Libertad\n"
    "3️⃣ Seguridad\n"
    "4️⃣ Control\n"
    "5️⃣ Las 4 dimensiones completas\n\n"
    "O escribe *menú* para volver al inicio."
)

DIMENSIONES_SALUD = {
    "resiliencia": {
        "nombre": "Resiliencia financiera",
        "emoji": "🛡️",
        "preguntas": [
            "Tengo el dinero suficiente para que nunca falte comida en mi casa.",
            "Tengo el dinero suficiente para cubrir gastos médicos míos o de mi familia si se presentan.",
            "Puedo gastar en compras pequeñas o regalos (una boda, un cumpleaños, etc.) sin que esto afecte mis finanzas.",
            "Si tengo un gasto imprevisto importante, puedo cubrirlo sin que mis finanzas se tambaleen.",
            "Si tuviera una emergencia económica, podría conseguir el dinero rápido para resolverla.",
        ],
        "rangos": [
            (5, 13, "🔴", "Baja resiliencia financiera",
             "Tienes dificultades para enfrentar imprevistos y cubrir tus necesidades básicas o emergencias "
             "financieras. Tus respuestas indican que no cuentas con los recursos suficientes para resistir "
             "contratiempos financieros."),
            (14, 19, "🟡", "Resiliencia financiera moderada",
             "Tienes cierta capacidad para hacer frente a imprevistos, pero aún hay áreas donde puedes mejorar. "
             "Podrías enfrentar problemas financieros en el futuro si no tomas precauciones."),
            (20, 25, "🟢", "Alta resiliencia financiera",
             "Demuestras una sólida capacidad para hacer frente a emergencias e imprevistos financieros. Estás "
             "bien preparado/a para manejar contratiempos sin comprometer tu estabilidad financiera."),
        ],
        "recomendacion_bajo": (
            "💡 Te podría servir mucho construir un fondo de emergencia. Dentro de *Ahorro* tengo una "
            "calculadora para definir tu meta de ahorro, y consejos prácticos para lograrlo sin sufrir en el "
            "intento."
        ),
        "recomendacion_alto": (
            "💡 Ya que tienes buena resiliencia, podrías aprovechar para que ese colchón de emergencia también "
            "genere rendimiento. Échale un ojo a *Inversión*, sobre todo a las opciones de bajo riesgo como "
            "CETES."
        ),
    },
    "libertad": {
        "nombre": "Libertad financiera",
        "emoji": "🕊️",
        "preguntas": [
            "En el último año pude hacer una compra grande (casa, terreno, coche, etc.) sin que esto desestabilizara mis finanzas.",
            "Tengo claras las metas que quiero lograr con mi dinero.",
            "Sé qué pasos seguir para llegar a mis metas financieras.",
            "Me siento seguro/a de que puedo lograr las metas financieras que me proponga.",
            "La forma en que manejo mi dinero me permite disfrutar la vida como quiero.",
        ],
        "rangos": [
            (5, 13, "🔴", "Baja libertad financiera",
             "Tienes poca libertad para disfrutar de tu vida o realizar gastos sin preocuparte por tu situación "
             "financiera. Sientes que no puedes obtener las cosas que deseas debido a limitaciones económicas."),
            (14, 19, "🟡", "Libertad financiera moderada",
             "Tienes cierta capacidad para disfrutar de tu vida y alcanzar metas financieras, pero aún tienes "
             "preocupaciones o limitaciones. Es posible hacer algunos gastos, pero no siempre con tranquilidad."),
            (20, 25, "🟢", "Alta libertad financiera",
             "Tienes una alta libertad financiera. Puedes realizar gastos importantes, disfrutar de tu vida y "
             "alcanzar tus metas financieras sin preocuparte por tu estabilidad económica."),
        ],
        "recomendacion_bajo": (
            "💡 Ponerte metas financieras claras puede ayudarte mucho aquí. Dentro de *Ahorro* tengo una "
            "calculadora para definir cuánto necesitas apartar para lograr una meta específica, y dentro de "
            "*Inversión* puedes ver cómo crecer tu dinero con el tiempo para metas más grandes."
        ),
        "recomendacion_alto": (
            "💡 Ya tienes buena claridad sobre tus metas. Podrías revisar *Jubilación* para asegurar que esa "
            "libertad se mantenga también a largo plazo."
        ),
    },
    "seguridad": {
        "nombre": "Seguridad financiera",
        "emoji": "🔐",
        "preguntas": [
            "En un mes normal, me alcanza para pagar todos mis gastos y recibos.",
            "Puedo pagar el lugar donde vivo (renta, hipoteca, etc.) sin que esto me ahogue económicamente.",
            "Aparto dinero para ahorrar de forma regular, mes con mes.",
            "Tengo ahorros que me alcanzarían para cubrir varios meses de gastos si los necesitara.",
            "Mi historial crediticio (buró de crédito) está en buen estado.",
            "No tengo que pedir dinero prestado para pagar otras deudas que ya tengo.",
            "Cuento con un seguro médico.",
        ],
        "rangos": [
            (7, 17, "🔴", "Baja seguridad financiera",
             "Tienes dificultades significativas para manejar tus finanzas de manera segura. Podrías tener "
             "problemas para cumplir con tus obligaciones financieras, gestionar deudas, o ahorrar para el "
             "futuro, lo que te deja vulnerable ante imprevistos."),
            (18, 26, "🟡", "Seguridad financiera moderada",
             "Tienes una seguridad financiera moderada. Estás gestionando tus finanzas relativamente bien, pero "
             "hay áreas que necesitan mejora. Eres capaz de cubrir tus obligaciones financieras básicas, pero "
             "podrías estar en riesgo si enfrentas situaciones inesperadas."),
            (27, 35, "🟢", "Alta seguridad financiera",
             "Demuestras una alta seguridad financiera. Eres capaz de cumplir con tus obligaciones financieras, "
             "tienes un buen historial crediticio, ahorras regularmente y estás preparado/a para imprevistos."),
        ],
        "recomendacion_bajo": (
            "💡 Dentro de *Crédito* tengo consejos para pagar sin ahogarte, cómo entender tu Buró de Crédito, y "
            "tus derechos frente al cobro de deudas. Y dentro de *Ahorro*, la calculadora de meta de ahorro te "
            "puede ayudar a construir un colchón para imprevistos."
        ),
        "recomendacion_alto": (
            "💡 Tienes una base sólida. Podrías revisar *Jubilación* para confirmar que también estás "
            "preparado/a a largo plazo."
        ),
    },
    "control": {
        "nombre": "Control financiero",
        "emoji": "🎯",
        "preguntas": [
            "Gasto menos de lo que gano.",
            "Llevo un control de en qué se me va el dinero.",
            "Logro ahorrar una parte de mis ingresos de forma regular, pensando en el futuro.",
            "No compro cosas por impulso de las que después me arrepiento.",
            "Entiendo cómo, cuando suben las tasas de interés, también suben los precios de las cosas.",
            "Sé que pagar solo el mínimo de mi tarjeta de crédito hace que me tarde mucho más en pagarla por completo.",
            "Sé a dónde acudir si necesito ayuda para tomar decisiones sobre mi dinero.",
            "Tengo metas financieras claras, tanto para el corto como para el largo plazo.",
        ],
        "rangos": [
            (8, 21, "🔴", "Bajo control financiero",
             "Tienes un bajo nivel de control sobre tus finanzas. Podrías no estar revisando tus ingresos y "
             "gastos de manera regular, tener dificultades para cumplir con un presupuesto, y ser propenso/a a "
             "realizar compras impulsivas o tomar malas decisiones financieras."),
            (22, 30, "🟡", "Control financiero moderado",
             "Tienes un control financiero aceptable pero con áreas de mejora. Aunque eres capaz de gestionar "
             "tus finanzas en cierta medida, puede haber ocasiones en las que pierdas el control de tus gastos o "
             "no sigas estrictamente un plan financiero."),
            (31, 40, "🟢", "Alto control financiero",
             "Tienes un alto control sobre tus finanzas. Mantienes un seguimiento claro de tus ingresos y "
             "gastos, sigues un presupuesto, ahorras regularmente y tomas decisiones financieras informadas."),
        ],
        "recomendacion_bajo": (
            "💡 Dentro del *Glosario* puedes repasar varios términos que mencionamos aquí. Y en *Crédito* tengo "
            "contenido sobre cómo identificar un crédito caro y errores comunes al pedir crédito, útil para "
            "tomar mejores decisiones."
        ),
        "recomendacion_alto": (
            "💡 Tienes muy buen control. Podrías profundizar en *Inversión*, en conceptos como diversificación, "
            "para seguir tomando decisiones informadas."
        ),
    },
}

ORDEN_DIMENSIONES_SALUD = ["resiliencia", "libertad", "seguridad", "control"]

def _resultado_dimension_salud(dim_key, puntaje):
    dim = DIMENSIONES_SALUD[dim_key]
    for minimo, maximo, color, etiqueta, descripcion in dim["rangos"]:
        if minimo <= puntaje <= maximo:
            recomendacion = dim["recomendacion_alto"] if color == "🟢" else dim["recomendacion_bajo"]
            return (
                f"{color} *{dim['emoji']} {dim['nombre']}: {etiqueta}* (puntaje: {puntaje})\n\n"
                f"{descripcion}\n\n"
                f"{recomendacion}"
            )
    # No debería pasar si el puntaje está dentro del rango posible, pero por seguridad:
    return f"{dim['emoji']} *{dim['nombre']}*: tu puntaje fue {puntaje}."

def _formatear_pregunta_salud(dim, idx, primera):
    total = len(dim["preguntas"])
    encabezado = f"{dim['emoji']} *{dim['nombre']}*, pregunta {idx + 1} de {total}"
    cuerpo = dim["preguntas"][idx]
    escala = (
        "Responde cada afirmación con un número del 1 al 5:\n"
        "1️⃣ Completamente en desacuerdo\n"
        "2️⃣ En desacuerdo\n"
        "3️⃣ Ni de acuerdo ni en desacuerdo\n"
        "4️⃣ De acuerdo\n"
        "5️⃣ Completamente de acuerdo"
    )
    return f"{encabezado}\n\n{cuerpo}\n\n{escala}"

mensaje_ahorro_consejos = (
    "💡 *Consejos para ahorrar sin sufrir en el intento*\n\n"
    "Ahorrar no tiene que sentirse como un sacrificio constante. Aquí van algunas ideas que te pueden ayudar a hacerlo de forma más simple y sostenible:\n"
    "________________________________________\n"
    "✅ 1. Crea un fondo de emergencia\n"
    "📌 Antes que cualquier otra meta, procura tener guardado entre 3 y 6 meses de tus gastos básicos.\n"
    "💡 Así, si algo imprevisto pasa (te quedas sin trabajo, se descompone algo importante), no tienes que endeudarte para resolverlo.\n"
    "________________________________________\n"
    "✅ 2. Prueba la regla 50/30/20\n"
    "📌 Una guía sencilla para organizar tu ingreso: 50% a tus gastos necesarios (renta, comida, transporte), 30% a tus gustos, y 20% a ahorro o pago de deudas.\n"
    "💡 No tiene que ser exacta, pero te da un punto de partida si no sabes por dónde empezar. Si quieres calcular tus montos exactos (y comparar contra lo que ya gastas), prueba la opción 4️⃣ Calculadora de presupuesto de este mismo menú.\n"
    "________________________________________\n"
    "✅ 3. Automatiza tu ahorro\n"
    "📌 Si tu banco lo permite, programa una transferencia automática a tu cuenta de ahorro justo cuando te paguen.\n"
    "💡 Así ahorras primero y gastas lo que sobra, en vez de ahorrar solo si sobra algo al final del mes.\n"
    "________________________________________\n"
    "✅ 4. Ponle nombre a tus metas\n"
    "📌 No es lo mismo ahorrar en general que ahorrar para algo específico (tu fondo de emergencia, un viaje, un enganche).\n"
    "💡 Tener metas claras te ayuda a mantenerte motivado/a y a no gastarte el dinero en otra cosa.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_ahorro

mensaje_ahorro_comparar_cuentas = (
    "🏦 *¿Dónde puedo comparar cuentas de ahorro entre bancos?*\n\n"
    "No todas las cuentas de ahorro son iguales: algunas dan más rendimiento que otras, y algunas cobran comisiones que se comen ese rendimiento.\n"
    "________________________________________\n"
    "📊 CONDUSEF, la Comisión Nacional para la Protección y Defensa de los Usuarios de Servicios Financieros, tiene información y comparadores gratuitos y oficiales sobre las tasas de distintos bancos e instituciones:\n"
    "🔗 https://www.condusef.gob.mx/\n"
    "________________________________________\n"
    "💡 Antes de abrir una cuenta nueva, vale la pena comparar al menos 2 o 3 opciones y revisar si cobran comisión por manejo de cuenta, porque eso también afecta cuánto realmente ganas.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_ahorro

mensaje_inversion_conceptos_basicos = (
    "📚 *Conceptos básicos antes de invertir*\n\n"
    "Antes de meter tu dinero en cualquier cosa, ayuda conocer estas ideas:\n"
    "________________________________________\n"
    "⚖️ 1. Riesgo y rendimiento van de la mano\n"
    "📌 Entre más alto el rendimiento que te prometen, generalmente más alto es el riesgo de perder tu dinero.\n"
    "💡 Si algo te ofrece ganancias garantizadas muy altas y rápidas, sé escéptico/a.\n"
    "________________________________________\n"
    "🧺 2. Diversifica\n"
    "📌 No pongas todo tu dinero en una sola opción. Repartirlo entre distintos instrumentos reduce el impacto si uno de ellos no funciona bien.\n"
    "________________________________________\n"
    "⏳ 3. Define tu horizonte de inversión\n"
    "📌 No es lo mismo invertir dinero que vas a necesitar en 6 meses que dinero que no vas a tocar en 10 años.\n"
    "💡 Para metas de corto plazo, conviene priorizar instrumentos de bajo riesgo y fácil acceso a tu dinero.\n"
    "________________________________________\n"
    "🔍 4. Entiende en qué estás invirtiendo\n"
    "📌 Si no entiendes cómo genera dinero un instrumento, es una señal para investigar más antes de invertir en él.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_inversion

mensaje_inversion_cetes = (
    "🏛️ *CETES y Cetesdirecto: invertir con bajo riesgo*\n\n"
    "Si buscas una opción de bajo riesgo para empezar a invertir, los CETES (Certificados de la Tesorería) son deuda del gobierno mexicano: en la práctica, le estás prestando dinero al gobierno a cambio de un interés.\n"
    "________________________________________\n"
    "📌 Se consideran de bajo riesgo porque están respaldados por el gobierno federal, aunque, como cualquier inversión, no están 100% libres de riesgo.\n"
    "📌 Puedes comprarlos directamente, sin intermediarios, desde la plataforma oficial del gobierno:\n"
    "🔗 https://www.cetesdirecto.com/\n"
    "📌 La inversión mínima es de $100 pesos, lo cual la hace accesible para casi cualquier persona que quiera empezar.\n"
    "________________________________________\n"
    "💡 Los CETES no son la única opción, pero son un buen punto de partida para entender cómo funciona invertir antes de explorar opciones con más riesgo.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_inversion

mensaje_inversion_fraudes = (
    "🚨 *Cómo identificar fraudes de inversión*\n\n"
    "Lamentablemente existen esquemas fraudulentos que se disfrazan de inversiones. Aquí algunas señales de alerta:\n"
    "________________________________________\n"
    "❌ 1. Te prometen rendimientos muy altos y garantizados\n"
    "📌 Ninguna inversión legítima puede garantizarte ganancias fijas y altas sin riesgo. Si suena demasiado bueno para ser verdad, probablemente lo sea.\n"
    "________________________________________\n"
    "❌ 2. Te presionan para decidir rápido\n"
    "📌 Frases como \"esta oportunidad es solo por hoy\" son una táctica común para que no investigues antes de invertir.\n"
    "________________________________________\n"
    "❌ 3. Te piden reclutar a más gente para ganar más\n"
    "📌 Si tus ganancias dependen más de que metas a otras personas que del rendimiento real de una inversión, probablemente es un esquema piramidal o Ponzi.\n"
    "________________________________________\n"
    "❌ 4. No están registrados ante las autoridades\n"
    "📌 Puedes verificar si una institución financiera está autorizada para operar en México directamente con CONDUSEF:\n"
    "🔗 https://www.condusef.gob.mx/\n"
    "________________________________________\n"
    "💡 Si algo no te queda claro o te da desconfianza, es válido decir que no. Nadie debería sentirse presionado a invertir su dinero.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_inversion

mensaje_jubilacion_afore = (
    "🏦 *¿Qué es una Afore y cómo saber en cuál estoy?*\n\n"
    "Una Afore (Administradora de Fondos para el Retiro) es la institución que administra el dinero que se va acumulando para tu pensión a lo largo de tu vida laboral: tus aportaciones, las de tu patrón, y las del gobierno.\n"
    "________________________________________\n"
    "📌 Si has trabajado de forma formal, con seguridad social, es muy probable que ya tengas una cuenta en alguna Afore, aunque nunca la hayas elegido tú mismo/a (a veces se asigna una automáticamente).\n"
    "📌 Puedes consultar en qué Afore estás de forma gratuita, con tu CURP o tu número de seguridad social, en el portal oficial de CONSAR:\n"
    "🔗 https://www.gob.mx/consar/acciones-y-programas/en-que-afore-estoy-56776\n"
    "________________________________________\n"
    "💡 Vale la pena revisarlo cada cierto tiempo, sobre todo si has cambiado de trabajo varias veces, para confirmar que tus aportaciones se estén acumulando correctamente.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_jubilacion

mensaje_jubilacion_ley73_vs_ley97 = (
    "📜 *¿Cómo se calcula mi pensión? Ley 73 vs. Ley 97*\n\n"
    "No todos calculamos nuestra pensión del IMSS de la misma manera: depende de cuándo te diste de alta por primera vez en el IMSS, no de tu edad ni de cuándo te vayas a retirar.\n"
    "________________________________________\n"
    "📅 Si te registraste ANTES del 1 de julio de 1997 (\"Ley 73\"):\n"
    "📌 Tu pensión se calcula con una fórmula del IMSS, basada en el salario promedio de tus últimos años cotizados y tus semanas trabajadas.\n"
    "📌 Necesitas al menos 500 semanas cotizadas.\n"
    "📌 Al retirarte, puedes elegir entre esa pensión o usar el dinero acumulado en tu Afore, lo que te convenga más.\n"
    "📌 Existe \"Modalidad 40\", que te permite seguir cotizando de forma voluntaria cerca del retiro para subir tu pensión. Vale la pena investigarlo si estás en este grupo.\n"
    "________________________________________\n"
    "📅 Si te registraste A PARTIR del 1 de julio de 1997 (\"Ley 97\"):\n"
    "📌 Tu pensión depende directamente de lo que se haya acumulado en tu cuenta individual de Afore (tus aportaciones, las de tu patrón, la cuota social del gobierno, y los rendimientos).\n"
    "📌 No tienes la opción de elegir una fórmula distinta: tu pensión es lo que junte tu Afore.\n"
    "📌 Las semanas mínimas cotizadas para pensionarte han ido subiendo cada año (en 2026 son 875, y seguirán subiendo hasta 1,000 en 2031), así que conviene confirmar la cifra vigente directamente con el IMSS.\n"
    "________________________________________\n"
    "💡 Un error común: tener una cuenta de Afore NO significa que automáticamente estés en Ley 97, ya que quienes están en Ley 73 también tienen una cuenta de Afore, aunque su PENSIÓN puede seguir calculándose con la fórmula anterior.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_jubilacion

mensaje_jubilacion_aportaciones_voluntarias = (
    "➕ *Aportaciones voluntarias: cómo aumentar tu ahorro para el retiro*\n\n"
    "Además de lo que se aporta obligatoriamente mientras trabajas, puedes meter dinero extra a tu cuenta Afore por tu cuenta, sin que nadie te obligue.\n"
    "________________________________________\n"
    "📌 A esto se le llama aportación voluntaria, y cualquier persona con una cuenta Afore puede hacerlo, sin importar si está en Ley 73 o Ley 97.\n"
    "📌 Ese dinero también genera rendimiento con el tiempo, igual que el resto de tu cuenta, así que entre antes empieces, más tiempo tiene para crecer.\n"
    "📌 Algunas aportaciones voluntarias pueden darte beneficios fiscales, como deducir parte de ese monto en tu declaración anual, dependiendo del tipo de aportación que elijas. Conviene confirmar los detalles vigentes directamente con tu Afore.\n"
    "________________________________________\n"
    "💡 No necesitas aportar grandes cantidades: aportar poco pero de forma constante también hace una diferencia real, gracias al interés compuesto, lo mismo que viste en la calculadora de esta sección.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_jubilacion

mensaje_jubilacion_cambio_trabajo = (
    "🔄 *¿Qué pasa si cambio de trabajo o dejo de cotizar?*\n\n"
    "Es una duda muy común, y la buena noticia es que tu dinero no se pierde.\n"
    "________________________________________\n"
    "📌 Tu cuenta Afore es tuya, no de tu empleador. Si cambias de trabajo, tu nueva empresa sigue aportando a la misma cuenta, identificada con tu CURP y tu número de seguridad social, no se abre una cuenta nueva.\n"
    "📌 Si te quedas sin empleo formal por un tiempo, tu dinero se queda guardado y sigue generando rendimiento, aunque nadie esté aportando en ese periodo.\n"
    "📌 Lo que sí puede verse afectado son tus semanas cotizadas, que en algunos casos son necesarias para calcular o tener derecho a tu pensión, así que procura no dejar pasar demasiado tiempo sin regularizar tu situación si puedes evitarlo.\n"
    "________________________________________\n"
    "💡 Si trabajas de forma independiente o informal por temporadas, existe la opción de seguir aportando de forma voluntaria a tu Afore para no perder continuidad.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_jubilacion

mensaje_jubilacion_independiente = (
    "🧑‍🌾 *No he trabajado de forma formal, ¿aún así puedo ahorrar para mi retiro?*\n\n"
    "Sí. Si nunca has estado dado de alta en el IMSS (por ejemplo, porque trabajas por tu cuenta, en el "
    "comercio informal, o de forma independiente), de todas formas puedes abrir tu propia cuenta para "
    "el retiro, sin necesidad de un patrón.\n"
    "________________________________________\n"
    "📌 Cualquier persona adulta con CURP puede abrir una cuenta Afore como \"trabajador independiente\", "
    "desde la aplicación Aforemóvil o desde Aforeweb.\n"
    "📌 No hay un monto ni un calendario fijo de aportación: metes dinero cuando puedes, en la cantidad "
    "que puedas.\n"
    "📌 Ese dinero también genera rendimiento con el tiempo, igual que las cuentas Afore ligadas a un "
    "trabajo formal.\n"
    "📌 Tus aportaciones voluntarias pueden ser deducibles de impuestos si las dejas guardadas hasta tu "
    "edad de retiro.\n"
    "________________________________________\n"
    "💡 No necesitas esperar a tener un trabajo formal para empezar a construir un ahorro para tu "
    "retiro. Entre antes empieces, aunque sea con poco, más tiempo tiene ese dinero para crecer.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_jubilacion

mensaje_credito_derechos_cobranza = (
    "⚖️ *Tus derechos frente al cobro de deudas*\n\n"
    "Deber dinero no te quita tus derechos. Existe una regla oficial de CONDUSEF que dice claramente "
    "qué SÍ y qué NO puede hacer un despacho de cobranza contigo.\n"
    "________________________________________\n"
    "✅ Lo que SÍ tienen permitido:\n"
    "📌 Llamarte para recordarte tu deuda, pero solo entre las 7:00 am y las 10:00 pm.\n"
    "📌 Identificarse contigo desde el primer contacto: su nombre, el despacho para el que trabajan, y "
    "a nombre de qué institución te están cobrando.\n"
    "________________________________________\n"
    "❌ Lo que NO tienen permitido:\n"
    "📌 Amenazarte, insultarte o intimidarte.\n"
    "📌 Llamarte desde un número oculto o privado.\n"
    "📌 Contactar a tu trabajo, familiares o conocidos para hablarles de tu deuda.\n"
    "📌 Hacerse pasar por una autoridad judicial, o amenazarte con un embargo sin tener realmente una "
    "orden de un juez.\n"
    "📌 Cobrarte una deuda que tú no reconoces como tuya.\n"
    "________________________________________\n"
    "🔍 Puedes verificar si un despacho de cobranza está registrado ante CONDUSEF aquí:\n"
    "🔗 https://eduweb.condusef.gob.mx/redeco/redeco.aspx\n"
    "📢 Y si sientes que te están cobrando de forma abusiva, puedes poner una queja directamente con "
    "CONDUSEF:\n"
    "🔗 https://www.condusef.gob.mx/\n"
    "________________________________________\n"
    "💡 Tener una deuda es una situación económica, no una razón para que alguien te trate mal. No "
    "tengas miedo de denunciar si algo así te pasa.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_credito

# =========================================
# Protege tus finanzas: seguros y fraudes
# =========================================
mensaje_submenu_proteccion = (
    "🛡️ *Protege tus finanzas: seguros y fraudes*\n\n"
    "1️⃣ Seguros: lo básico que debes saber\n"
    "2️⃣ Fraudes financieros más comunes (y cómo protegerte)\n"
    "3️⃣ Ya soy víctima de un fraude, ¿qué hago?\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_proteccion_seguros = (
    "🛡️ *Seguros: lo básico que debes saber*\n\n"
    "Un seguro no es un gasto para cuando te va bien: es una herramienta para que un imprevisto caro "
    "(un accidente, una enfermedad grave, un choque) no se lleve tus ahorros o te deje endeudado/a.\n"
    "________________________________________\n"
    "📖 *Términos que vas a ver siempre*:\n"
    "📌 *Prima*: lo que pagas (mensual, anual, etc.) por tener el seguro activo.\n"
    "📌 *Suma asegurada*: el monto máximo que la aseguradora cubre.\n"
    "📌 *Deducible*: lo que pagas tú primero, antes de que la aseguradora cubra el resto.\n"
    "📌 *Coaseguro*: un porcentaje del gasto que sigues pagando tú, incluso después del deducible.\n"
    "📌 *Exclusiones*: lo que el seguro NO cubre. Revísalas siempre antes de firmar.\n"
    "________________________________________\n"
    "🧩 *Los básicos*:\n"
    "🏥 *Gastos médicos mayores*: cubre hospitalización y cirugías costosas. El IMSS/ISSSTE ya te cubre "
    "esto si eres derechohabiente, pero con tiempos de espera largos; si trabajas por tu cuenta, "
    "probablemente no tengas ninguna cobertura de este tipo.\n"
    "❤️ *Seguro de vida*: protege económicamente a quien depende de tu ingreso si tú faltas. Si nadie "
    "depende de tu ingreso, es menos urgente para ti.\n"
    "🚗 *Seguro de auto*: cubre daños a otras personas o autos (en varios estados es obligatorio) y, si "
    "lo agregas, daños a tu propio coche o robo.\n"
    "🏠 *Seguro de hogar*: cubre daños por incendio y, según la póliza, robo o fenómenos naturales.\n"
    "________________________________________\n"
    "❌ *Mitos comunes*:\n"
    "🚫 \"Estoy sano/a, no lo necesito\": el seguro no es para cuando estás bien, es para el día que no "
    "lo estés y no puedas pagarlo de tu bolsillo.\n"
    "🚫 \"Ya tengo IMSS/ISSSTE, no necesito nada más\": te cubre lo básico, pero no siempre con la "
    "rapidez o el hospital que necesitarías en una emergencia.\n"
    "________________________________________\n"
    "🔍 Antes de contratar, no te quedes solo con la prima más barata: compara también el deducible, "
    "las exclusiones y la red de hospitales o talleres. CONDUSEF tiene simuladores gratuitos (por "
    "ejemplo, de seguro de auto) en:\n"
    "🔗 https://www.condusef.gob.mx/?idc=1635&idcat=1&p=contenido\n"
    "________________________________________\n"
    "💡 Regla práctica: si perder algo te sacaría de tu presupuesto por meses o años (tu salud, tu "
    "capacidad de generar ingresos, tu patrimonio), vale la pena asegurarlo. Si solo te incomodaría un "
    "rato, probablemente no necesitas seguro para eso.\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_proteccion

mensaje_proteccion_fraudes = (
    "🚨 *Fraudes financieros más comunes (y cómo protegerte)*\n\n"
    "📊 Las quejas por fraude financiero en México subieron 31.5% en el primer trimestre de 2026 frente "
    "al mismo periodo de 2025, con más de 1.5 millones de casos y 5,201 millones de pesos reclamados; "
    "los bancos solo reembolsaron 24.3% de ese monto (Condusef, vía El Universal, julio 2026). México es "
    "el segundo país más afectado por fraude digital a nivel mundial.\n"
    "________________________________________\n"
    "🎭 *Las tácticas más comunes*:\n"
    "📞 *Vishing (llamada falsa de tu banco)*: te llaman diciendo que hay un cargo sospechoso y te "
    "piden \"confirmar\" tu tarjeta, tu NIP o un código que te llegó por SMS. Tu banco NUNCA te pide "
    "esos datos por teléfono.\n"
    "📱 *Smishing (SMS o WhatsApp con enlaces falsos)*: mensajes como \"tienes una recompensa\" o "
    "\"tu paquete está retenido\", con un link que roba tus datos al abrirlo.\n"
    "💸 *Préstamos o créditos falsos*: apps o personas que ofrecen un préstamo \"inmediato, sin "
    "requisitos\", pero primero te piden un pago \"de garantía\" o \"para activarlo\"; después "
    "desaparecen o te extorsionan.\n"
    "💳 *Clonación de tarjeta*: copian los datos de tu tarjeta en un cajero o al pagar. Revisa tus "
    "movimientos seguido y activa las notificaciones de cada compra.\n"
    "🤖 *Fraude con inteligencia artificial*: te llaman o mandan un audio con la voz clonada de un "
    "familiar pidiendo dinero urgente. Cuelga y confírmalo por otro medio antes de transferir nada.\n"
    "________________________________________\n"
    "🚩 *Señales de alerta*:\n"
    "📌 Nadie legítimo (tu banco, el SAT, CONDUSEF) te va a pedir tu NIP, CVV, contraseña o un código "
    "que te llegó por SMS. Nunca.\n"
    "📌 Desconfía de la urgencia (\"tu cuenta se bloqueará en 10 minutos\").\n"
    "📌 No des clic en links de mensajes inesperados; entra directamente escribiendo tú la dirección "
    "de tu banco.\n"
    "📌 Si te llaman de \"tu banco\", cuelga y marca tú al número oficial (el de atrás de tu tarjeta).\n"
    "________________________________________\n"
    "🆕 En 2026, CONDUSEF lanzó una herramienta para consultar y reportar números telefónicos ya "
    "identificados en fraudes, antes de compartir cualquier dato. Está disponible en:\n"
    "🔗 https://www.condusef.gob.mx/\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_proteccion

mensaje_proteccion_que_hacer = (
    "🆘 *Ya soy víctima de un fraude, ¿qué hago?*\n\n"
    "1️⃣ Llama de inmediato a tu banco al número oficial (el de atrás de tu tarjeta, no uno que te "
    "hayan dado por teléfono) para bloquear tu cuenta o tarjeta.\n"
    "2️⃣ Levanta una aclaración formal por escrito ante tu banco: están obligados a investigarla.\n"
    "3️⃣ Si tu banco no resuelve o rechaza tu reclamo, acude a CONDUSEF, que puede mediar entre tú y la "
    "institución:\n"
    "☎️ 55 5340 0999, o sin costo al 800 999 8080 (lunes a viernes, 8:30 a 16:00 h)\n"
    "✉️ asesoria@condusef.gob.mx\n"
    "🔗 https://www.condusef.gob.mx/\n"
    "4️⃣ Si el fraude fue con una app o negocio falso (no un banco), también puedes denunciar ante la "
    "Fiscalía o la Guardia Nacional (Policía Cibernética).\n"
    "5️⃣ Guarda evidencia: capturas de pantalla, números de teléfono y mensajes; te los van a pedir.\n"
    "________________________________________\n"
    "💡 Los bancos solo devuelven una fracción de lo reclamado, así que prevenir vale mucho más que "
    "reclamar después (revisa la opción 2 de esta sección, sobre las tácticas de fraude más comunes).\n"
    "________________________________________\n"
) + "\n" + mensaje_submenu_proteccion

GLOSARIO_TERMINOS = [
    (["afore"], "Afore",
     "La institución que administra el dinero que se va acumulando para tu pensión (Administradora de Fondos para el Retiro)."),
    (["buró de crédito", "buro de credito"], "Buró de Crédito",
     "Una empresa que guarda tu historial de pagos de créditos. Si pagas bien, tu historial ayuda a que te aprueben créditos en el futuro; si te atrasas, se refleja ahí."),
    (["capacidad de pago"], "Capacidad de pago",
     "Cuánto dinero de tu ingreso te queda disponible cada mes, después de tus gastos y deudas actuales, para poder pagar un crédito nuevo sin ahogarte."),
    (["cat", "costo anual total"], "CAT (Costo Anual Total)",
     "Un número que junta la tasa de interés más las comisiones de un crédito, para que puedas comparar qué tan caro es de verdad. Entre más alto el CAT, más caro te sale el crédito."),
    (["cetes"], "CETES",
     "Certificados de la Tesorería: deuda del gobierno mexicano. Al comprarlos, básicamente le prestas dinero al gobierno a cambio de un interés. Se consideran de bajo riesgo."),
    (["coaseguro"], "Coaseguro",
     "En un seguro, el porcentaje del gasto que sigues pagando tú, incluso después de cubrir el deducible."),
    (["cuota social"], "Cuota social",
     "Una aportación extra que da el gobierno a tu cuenta Afore, además de lo que aportas tú y tu patrón."),
    (["declaración anual", "declaracion anual"], "Declaración anual",
     "El trámite que haces ante el SAT (normalmente en abril) para reportar tus ingresos y deducciones del año. Si te retuvieron más impuesto del que debías, aquí es donde puedes recuperar la diferencia."),
    (["deducción personal", "deduccion personal", "deducciones personales"], "Deducción personal",
     "Un gasto que la ley te permite restar de tus ingresos antes de calcular tus impuestos (por ejemplo, gastos médicos o colegiaturas), siempre que lo hayas pagado con transferencia, tarjeta o cheque y tengas tu factura."),
    (["deducible"], "Deducible (seguro)",
     "En un seguro, lo que pagas tú primero de tu bolsillo antes de que la aseguradora empiece a cubrir el resto del gasto."),
    (["deuda revolvente"], "Deuda revolvente",
     "Una deuda sin fecha fija para terminarse, como una tarjeta de crédito: vas pagando lo que usas cada mes, y puedes seguir usando el crédito disponible."),
    (["diversificar", "diversificación"], "Diversificar",
     "No poner todo tu dinero en una sola opción de inversión, para que si una no funciona bien, no pierdas todo."),
    (["ingreso neto", "ingreso mensual neto"], "Ingreso neto",
     "Lo que realmente recibes de dinero después de impuestos: lo que te depositan o te dan en efectivo."),
    (["interés compuesto"], "Interés compuesto",
     "Cuando el interés que ganas (o debes) también genera más interés con el tiempo, no solo el dinero original. Por eso el dinero puede crecer mucho más mientras más tiempo lo dejes invertido."),
    (["isr", "impuesto sobre la renta"], "ISR (Impuesto Sobre la Renta)",
     "El impuesto que pagas sobre el dinero que ganas, ya sea tu sueldo o las ganancias de tu negocio. Entre más ganas, mayor es el porcentaje que te toca pagar."),
    (["ley 73"], "Ley 73",
     "Las reglas para calcular la pensión de quienes se registraron en el IMSS ANTES del 1 de julio de 1997."),
    (["ley 97"], "Ley 97",
     "Las reglas para calcular la pensión de quienes se registraron en el IMSS A PARTIR del 1 de julio de 1997."),
    (["modalidad 40"], "Modalidad 40",
     "Una opción para seguir aportando al IMSS de forma voluntaria cerca de tu retiro (solo aplica si estás en Ley 73), para intentar subir el monto de tu pensión."),
    (["phishing", "vishing", "smishing"], "Phishing",
     "Un intento de engaño (por llamada, SMS, WhatsApp o correo) donde alguien se hace pasar por tu banco u otra institución para sacarte datos personales o dinero. Ningún banco te pide tu NIP, CVV o contraseña por estos medios."),
    (["prima"], "Prima (seguro)",
     "Lo que pagas de forma periódica (mensual, anual, etc.) para mantener un seguro activo."),
    (["resico", "régimen simplificado de confianza", "regimen simplificado de confianza"], "RESICO (Régimen Simplificado de Confianza)",
     "Un régimen fiscal sencillo para personas físicas con ingresos de hasta $3,500,000 al año, donde pagas una tasa baja (de 1% a 2.5%) sobre todo lo que facturas, sin tener que restar tus gastos."),
    (["semanas cotizadas"], "Semanas cotizadas",
     "El número de semanas que has trabajado de forma formal (registrado en el IMSS). Se necesita un mínimo de semanas cotizadas para tener derecho a una pensión."),
    (["suma asegurada"], "Suma asegurada",
     "El monto máximo que un seguro cubre en caso de que ocurra lo que estás asegurando."),
    (["tasa de interés", "tasa anual", "tasa periodo", "tasa por periodo"], "Tasa de interés",
     "El porcentaje que te cobran (si pides prestado) o que te pagan (si ahorras/inviertes) sobre el dinero, normalmente expresado por año."),
    (["uma"], "UMA (Unidad de Medida y Actualización)",
     "Un valor en pesos que el gobierno actualiza cada año, y que se usa como referencia para calcular distintos límites y montos en trámites oficiales, incluyendo temas de pensiones."),
]

mensaje_glosario = (
    "📖 *Glosario de términos financieros*\n\n"
    "Aquí te explico en palabras simples algunos términos que uso en este bot:\n"
    "________________________________________\n"
    + "\n________________________________________\n".join(
        f"🔑 *{nombre}*\n{explicacion}"
        for _, nombre, explicacion in GLOSARIO_TERMINOS
    )
    + "\n________________________________________\n"
    "💡 Si en cualquier momento de la conversación no entiendes algo que te escribí, puedes escribir "
    "*explícamelo más fácil* y trato de aclarártelo.\n\n"
    "Escribe *menú* para volver al inicio."
)

def buscar_terminos_glosario(texto):
    """
    Busca, dentro de un texto (normalmente el último mensaje que envió el
    bot), qué términos del glosario aparecen mencionados, para poder
    explicarlos de forma más sencilla cuando alguien lo pida.
    """
    if not texto:
        return []
    texto_normalizado = texto.lower()
    encontrados = []
    for patrones, nombre, explicacion in GLOSARIO_TERMINOS:
        if any(patron in texto_normalizado for patron in patrones):
            encontrados.append((nombre, explicacion))
    return encontrados

mensaje_creditos = (
    "👩‍🏫 ¿Quiénes hicimos este bot?\n\n"
    "Este proyecto es obra de un equipo de académicas de la Facultad de Ciencias Administrativas de "
    "la UABC, unidas por la misión de acercar la educación financiera a cualquier persona, tenga "
    "poca o mucha experiencia previa con temas de dinero.\n"
    "________________________________________\n"
    "✍️ Dra. Ana Jazmín Sandoval Sánchez\n"
    "Autora y creadora de este bot, y Líder del Cuerpo Académico Gestión Disruptiva, Cooperación e "
    "Inclusión en Organizaciones y Comunidades.\n"
    "________________________________________\n"
    "🌟 Dra. Sósima Carrillo\n"
    "Coautora de este proyecto, Líder del Cuerpo Académico Gestión Financiera y Administrativa de las "
    "Organizaciones. Su mentoría y su compromiso genuino con la educación financiera han sido una "
    "inspiración fundamental para que este proyecto exista.\n"
    "________________________________________\n"
    "🤝 Dras. Yésica Lizbet Benítez Niebla, Paulina Villalobos Torres y Zyanya María Villa Zamorano\n"
    "Coautoras de este proyecto e integrantes del Cuerpo Académico Gestión Disruptiva, Cooperación e "
    "Inclusión en Organizaciones y Comunidades. Su entusiasmo y compromiso por impulsar siempre ideas "
    "disruptivas y diferentes son parte esencial de la misión que compartimos: contribuir, desde "
    "nuestro trabajo, a cambiar al mundo.\n"
    "________________________________________\n"
    "Gracias por confiar en este proyecto 💚\n"
    "Escribe *menú* para volver."
)

def normalizar_numero(numero):
    """
    WhatsApp reporta los números mexicanos en los webhooks entrantes con un
    "1" extra después del 52 (ej. 521XXXXXXXXXX), pero la API espera el
    número SIN ese 1 al enviar mensajes (ej. 52XXXXXXXXXX). Si no se quita,
    el envío falla con el error 131030 "Recipient phone number not in
    allowed list", aunque el número sí esté autorizado.
    """
    if numero.startswith("521") and len(numero) == 13:
        return "52" + numero[3:]
    return numero

# WhatsApp rechaza (error 400) cualquier mensaje de texto de más de 4096
# caracteres. Como el bot va creciendo (glosario, secciones nuevas, etc.),
# algún mensaje puede llegar a superar ese límite; en vez de que falle el
# envío, lo partimos en varios mensajes.
LIMITE_TEXTO_WHATSAPP = 4096

_SEPARADOR_SECCIONES = "\n________________________________________\n"

def _dividir_mensaje_largo(texto, limite=LIMITE_TEXTO_WHATSAPP):
    """
    Si hace falta partir el mensaje, primero intenta cortar justo en uno de
    los separadores "________" que ya se usan entre secciones/términos, para
    no partir un título de su explicación a la mitad; si no encuentra uno
    cerca, corta en el último salto de línea antes del límite.
    """
    if len(texto) <= limite:
        return [texto]
    partes = []
    restante = texto
    while len(restante) > limite:
        corte = restante.rfind(_SEPARADOR_SECCIONES, 0, limite)
        if corte > 0:
            corte += 1  # deja el separador al inicio de la siguiente parte
        else:
            corte = restante.rfind("\n", 0, limite)
        if corte <= 0:
            corte = limite
        partes.append(restante[:corte].rstrip())
        restante = restante[corte:].lstrip("\n")
    if restante:
        partes.append(restante)
    return partes

def enviar_mensaje(numero, texto):
    numero = normalizar_numero(numero)
    for parte in _dividir_mensaje_largo(texto):
        _enviar_mensaje_whatsapp(numero, parte)

def _enviar_mensaje_whatsapp(numero, texto):
    print(f"[Enviar a {numero}]: {texto}")
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {
            "body": texto
        }
    }
    try:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code == 200:
            print(f"✅ Mensaje enviado a {numero}")
        else:
            print(f"❌ Error al enviar mensaje a {numero}")
        print(response.status_code)
        print(response.text)
    except Exception as e:
        print("❌ Error al enviar mensaje:", e)

# =========================================
# Retroalimentación (👍/👎) después de una calculadora
# =========================================
# Varios resultados de calculadoras ya terminan con esta frase; si la
# dejáramos, quedaría rara justo antes de la pregunta de retroalimentación.
_FRASE_MENU_FINAL_RE = re.compile(r'\n*Escribe \*menú\* para volver al inicio\.\s*$')

def _con_feedback(numero, calculadora, texto_resultado, estado_regreso=None, mensaje_regreso=None):
    """
    Agrega una pregunta de retroalimentación (👍/👎) al final del resultado de
    una calculadora, y deja a la persona en un estado especial para leer su
    respuesta (y registrarla de forma anónima en la analítica de uso, ver
    _registrar_evento_uso) antes de continuar. estado_regreso/mensaje_regreso
    son el estado y el submenú que se mostraban normalmente después de este
    resultado; si son None, después del feedback simplemente se limpia la
    conversación, igual que antes de agregar esto.
    """
    estado_usuario[numero] = {
        "esperando": "feedback_resultado",
        "feedback_calculadora": calculadora,
        "feedback_estado_regreso": estado_regreso,
        "feedback_mensaje_regreso": mensaje_regreso,
    }
    texto_sin_cierre = _FRASE_MENU_FINAL_RE.sub('', texto_resultado).rstrip()
    return (
        texto_sin_cierre
        + "\n\n________________________________________\n"
        + "🙏 ¿Te resultó útil este resultado? Responde 👍 o 👎 (o escribe *menú* para salir)."
    )

def _procesar_mensaje_interno(mensaje, numero):
    texto_limpio = _MODIFICADORES_EMOJI_RE.sub('', _BORDE_PUNTUACION_RE.sub('', mensaje).lower())

    # Estados donde una respuesta numérica (ej. "1", "36") no debe confundirse
    # con los accesos directos del menú principal.
    subflujo_critico = False
    if numero in estado_usuario:
        esperando = estado_usuario[numero].get("esperando")
        if esperando in [
            "desde_cuando1", "desde2",
            "abono_extra1", "abono_extra2",
            "riesgo", "subopcion_prestamo",
            "submenu_despues_de_maximo",
            "tasa_anual_credito", "anios_credito", "frecuencia_credito", "frecuencia_otro_credito",
            "tasa_anual2", "anios2", "frecuencia2", "frecuencia_otro2",
            "tasa_anual_simular", "anios_simular", "frecuencia_simular", "frecuencia_otro_simular",
            "tasa_anual_deseada", "anios_deseado", "frecuencia_deseada", "frecuencia_otro_deseada",
            "menu_ahorro", "menu_credito", "menu_inversion", "menu_jubilacion",
            "ahorro_meta", "ahorro_inicial", "ahorro_tiempo_numero", "ahorro_tiempo_unidad",
            "ahorro_frecuencia", "ahorro_frecuencia_otro",
            "inversion_monto_inicial", "inversion_aportacion", "inversion_tasa_anual",
            "inversion_tiempo_numero", "inversion_tiempo_unidad",
            "inversion_frecuencia", "inversion_frecuencia_otro",
            "menu_jubilacion_calculadoras",
            "jubilacion_saldo_actual", "jubilacion_edad_actual", "jubilacion_edad_retiro",
            "jubilacion_genero", "jubilacion_aportacion_mensual", "jubilacion_rendimiento_anual",
            "imss_saldo_actual", "imss_edad_actual", "imss_edad_retiro", "imss_genero",
            "imss_salario_mensual", "imss_rendimiento_anual",
            "imss_aportacion_voluntaria_mensual",
            "issste_saldo_actual", "issste_edad_actual", "issste_edad_retiro", "issste_genero",
            "issste_sueldo_basico_mensual", "issste_ahorro_solidario", "issste_bono_pension",
            "issste_rendimiento_anual",
            "ley73_salario_promedio", "ley73_semanas_cotizadas", "ley73_edad_retiro",
            "menu_salud", "salud_pregunta", "menu_genero",
            "menu_emprendedor", "emprendedor_unidades", "emprendedor_costo_unitario",
            "emprendedor_costos_fijos", "emprendedor_utilidad_deseada", "emprendedor_precio_prueba",
            "empc_frecuencia", "empc_frecuencia_otro",
            "empc_unidades", "empc_costo_unitario", "empc_pct_variable", "empc_costos_fijos",
            "empc_depreciacion", "empc_tiene_credito", "empc_credito_monto", "empc_credito_tasa",
            "empc_credito_plazo", "empc_tasa_impositiva", "empc_utilidad_deseada", "empc_precio_prueba",
            "turismo_capacidad", "turismo_ocupacion", "turismo_costos_fijos", "turismo_costo_variable",
            "turismo_comision", "turismo_utilidad_deseada", "turismo_precio_prueba",
            "turismo_quiere_mensual", "turismo_tours_mensuales", "turismo_costos_fijos_mensuales",
            "menu_impuestos", "impuestos_isr_sueldo", "impuestos_resico_ingreso", "impuestos_resico_gastos",
            "menu_proteccion", "feedback_resultado",
            "pago_minimo_saldo", "pago_minimo_limite", "pago_minimo_tasa",
            "deudas_cantidad", "deudas_saldo", "deudas_tasa", "deudas_pago", "deudas_extra",
            "presupuesto_ingreso", "presupuesto_comparar",
            "presupuesto_gasto_necesidades", "presupuesto_gasto_gustos",
        ]:
            subflujo_critico = True

    # "menú" es la salida de emergencia: funciona incluso en medio de un
    # subflujo crítico, porque nunca es un dato válido que se esté pidiendo.
    if texto_limpio in ["menu", "menú"]:
        estado_usuario[numero] = {}
        return saludo_inicial

    # ======================
    # MENÚ PRINCIPAL 1..10 + "equipo"
    # ======================
    if not subflujo_critico:
        if texto_limpio == "hola":
            estado_usuario[numero] = {}
            return saludo_inicial

        if texto_limpio in ["1", "ahorro"]:
            estado_usuario[numero] = {"esperando": "menu_ahorro"}
            return mensaje_submenu_ahorro

        if texto_limpio in ["2", "credito", "crédito"]:
            estado_usuario[numero] = {"esperando": "menu_credito"}
            return mensaje_submenu_credito

        if texto_limpio in ["3", "inversion", "inversión"]:
            estado_usuario[numero] = {"esperando": "menu_inversion"}
            return mensaje_submenu_inversion

        if texto_limpio in ["4", "jubilacion", "jubilación"]:
            estado_usuario[numero] = {"esperando": "menu_jubilacion"}
            return mensaje_submenu_jubilacion

        if texto_limpio in [
            "5", "herramientas para el emprendedor", "herramientas para emprendedores",
            "emprendedor", "emprendedores",
        ]:
            estado_usuario[numero] = {"esperando": "menu_emprendedor"}
            return mensaje_submenu_emprendedor

        if texto_limpio in ["6", "género y finanzas", "genero y finanzas"]:
            estado_usuario[numero] = {"esperando": "menu_genero"}
            return mensaje_submenu_genero

        if texto_limpio in [
            "7", "impuestos", "impuestos y cómo afectan tus finanzas", "impuestos y como afectan tus finanzas",
        ]:
            estado_usuario[numero] = {"esperando": "menu_impuestos"}
            return mensaje_submenu_impuestos

        if texto_limpio in [
            "8", "protege tus finanzas", "protege tus finanzas: seguros y fraudes",
            "seguros y fraudes", "seguros", "fraudes",
        ]:
            estado_usuario[numero] = {"esperando": "menu_proteccion"}
            return mensaje_submenu_proteccion

        if texto_limpio in [
            "9", "evalúa tu salud financiera", "evalua tu salud financiera",
            "evaluar mi salud financiera", "salud financiera",
        ]:
            estado_usuario[numero] = {"esperando": "menu_salud"}
            return mensaje_submenu_salud

        if texto_limpio in ["10", "glosario", "glosario de términos financieros", "glosario de terminos financieros"]:
            estado_usuario[numero] = {}
            return mensaje_glosario

        if texto_limpio in [
            "11", "equipo", "quiénes hicimos este bot", "¿quiénes hicimos este bot?", "quienes hicimos este bot",
        ]:
            estado_usuario[numero] = {}
            return mensaje_creditos

        # Accesos directos por nombre exacto de cada herramienta, para quien ya conoce el bot
        # y prefiere escribirlo directamente sin pasar por los submenús.
        if texto_limpio in ["simular un crédito", "simular crédito"]:
            estado_usuario[numero] = {"esperando": "monto_credito"}
            return "Perfecto. Para comenzar, dime el monto del crédito que deseas simular."

        if texto_limpio in ["ahorro con pagos extra", "ver cuánto me ahorro si doy pagos extra al crédito"]:
            estado_usuario[numero] = {"esperando": "monto2"}
            return "Para estimar tu ahorro con pagos extra, primero dime el Monto del crédito."

        if texto_limpio in [
            "costo real de compras a meses", "costo real de comprar a plazos en tiendas",
            "calcular el costo real de compras a pagos fijos en tiendas departamentales",
        ]:
            estado_usuario[numero] = {"esperando": "precio_contado"}
            return (
                "Vamos a calcular el costo real de una compra a pagos fijos.\n"
                "Por favor dime lo siguiente:\n\n"
                "1️⃣ ¿Cuál es el precio de contado del producto? (ejemplo: 1800)"
            )

        if texto_limpio in [
            "pago mínimo de tu tarjeta", "pago minimo de tu tarjeta",
            "cómo se calcula el pago mínimo", "como se calcula el pago minimo",
        ]:
            estado_usuario[numero] = {"esperando": "pago_minimo_saldo"}
            return mensaje_intro_pago_minimo

        if texto_limpio in [
            "plan para pagar varias deudas", "bola de nieve", "avalancha",
            "plan para pagar mis deudas", "pagar varias deudas",
        ]:
            estado_usuario[numero] = {"esperando": "deudas_cantidad"}
            return mensaje_intro_plan_deudas

        if texto_limpio in [
            "calculadora de presupuesto", "regla 50/30/20", "50/30/20", "presupuesto",
        ]:
            estado_usuario[numero] = {"esperando": "presupuesto_ingreso"}
            return mensaje_intro_presupuesto

        if texto_limpio in ["cuánto me pueden prestar", "¿cuánto me pueden prestar?"]:
            estado_usuario[numero] = {"esperando": "ingreso"}
            return (
                "Vamos a calcular cuánto podrías solicitar como crédito, según tu capacidad de pago.\n\n"
                "Primero necesito saber:\n"
                "1️⃣ ¿Cuál es tu ingreso mensual neto? Es decir, lo que realmente recibes después de "
                "impuestos: lo que te depositan o te dan en efectivo. (ejemplo: 15000)"
            )

        if texto_limpio in ["consejos para pagar sin ahogarte", "consejos para pagar un crédito sin ahogarte"]:
            return (
                "🟡 Consejos para pagar un crédito sin ahogarte\n"
                "Pagar un crédito no tiene que sentirse como una carga eterna. Aquí van algunos consejos sencillos para ayudarte a pagar con más tranquilidad y menos estrés:\n"
                "________________________________________\n"
                "✅ 1. Haz pagos anticipados cuando puedas\n"
                "📌 Aunque no sea obligatorio, abonar un poco más al capital te ahorra intereses y reduce el plazo.\n"
                "💡 Incluso $200 o $500 adicionales hacen una gran diferencia con el tiempo.\n"
                "________________________________________\n"
                "✅ 2. Programa tus pagos en automático\n"
                "📌 Evitas atrasos, recargos y estrés.\n"
                "💡 Si no tienes domiciliación, pon recordatorios para no fallar.\n"
                "________________________________________\n"
                "✅ 3. Revisa si puedes cambiar tu crédito por uno mejor\n"
                "📌 A esto se le llama “reestructura” o “portabilidad”.\n"
                "💡 Si tu historial ha mejorado, podrías conseguir mejores condiciones.\n"
                "________________________________________\n"
                "✅ 4. Haz un presupuesto mensual\n"
                "📌 Saber cuánto entra y cuánto sale te ayuda a organizar tus pagos sin descuidar otras necesidades.\n"
                "💡 Apóyate en apps, papel o Excel, lo que te funcione.\n"
                "________________________________________\n"
                "✅ 5. Prioriza las deudas más caras\n"
                "📌 Si tienes varias, enfócate primero en las que tienen interés más alto, como tarjetas de crédito.\n"
                "________________________________________\n"
                "Escribe *menú* para volver."
            )

        if texto_limpio in ["identificar un crédito caro", "cómo identificar un crédito caro"]:
            return (
                "Muchas veces un crédito parece accesible… hasta que ves lo que terminas pagando. Aquí te doy algunas claves para detectar si un crédito es caro:\n\n"
                "🔍 1. CAT (Costo Anual Total)\n"
                "Es una medida que incluye la tasa de interés, comisiones y otros cargos.\n"
                "📌 Entre más alto el CAT, más caro te saldrá el crédito.\n"
                "💡 Compara el CAT entre diferentes instituciones, no solo la tasa.\n\n"
                "🔍 2. Comisiones escondidas\n"
                "Algunos créditos cobran por apertura, por manejo, por pagos tardíos o por pagos anticipados 😵\n"
                "📌 Lee siempre el contrato antes de firmar.\n\n"
                "🔍 3. Tasa de interés variable\n"
                "📌 Algunos créditos no tienen tasa fija, sino que pueden subir.\n"
                "💡 Revisa si tu tasa es fija o variable. Las variables pueden volverse muy caras si sube la inflación.\n\n"
                "🔍 4. Pago mensual bajo con plazo largo\n"
                "Parece atractivo, pero terminas pagando muchísimo más en intereses.\n\n"
                "❗ Si el crédito parece demasiado fácil o rápido, pero no entiendes bien cuánto vas a pagar en total... ¡es una señal de alerta!\n\n"
                "Escribe *menú* para volver."
            )

        if texto_limpio in ["errores comunes al pedir crédito", "errores comunes al solicitar un crédito"]:
            return (
                "Solicitar un crédito es una gran responsabilidad. Aquí te comparto algunos errores comunes que muchas personas cometen… ¡y cómo evitarlos!\n"
                "________________________________________\n"
                "❌ 1. No saber cuánto terminarás pagando en total\n"
                "Muchas personas solo se fijan en el pago mensual y no en el costo total del crédito.\n"
                "✅ Usa simuladores (como el que tengo 😎) para saber cuánto pagarás realmente.\n"
                "________________________________________\n"
                "❌ 2. Pedir más dinero del que realmente necesitas\n"
                "📌 Entre más pidas, más intereses pagas.\n"
                "✅ Pide solo lo necesario y asegúrate de poder pagarlo.\n"
                "________________________________________\n"
                "❌ 3. Aceptar el primer crédito que te ofrecen\n"
                "📌 Hay diferencias enormes entre una institución y otra.\n"
                "✅ Compara tasas, comisiones y condiciones antes de decidir.\n"
                "________________________________________\n"
                "❌ 4. No leer el contrato completo\n"
                "Sí, puede ser largo, pero ahí están los detalles importantes:\n"
                "📌 ¿Hay comisiones por pagar antes de tiempo?\n"
                "📌 ¿Qué pasa si te atrasas?\n"
                "✅ Lee con calma o pide que te lo expliquen.\n"
                "________________________________________\n"
                "❌ 5. Usar un crédito sin un plan de pago\n"
                "📌 Si no sabes cómo lo vas a pagar, puedes meterte en problemas.\n"
                "✅ Haz un presupuesto antes de aceptar cualquier crédito.\n\n"
                "Escribe *menú* para volver."
            )

        if texto_limpio in ["entender el buró de crédito"]:
            estado_usuario[numero] = {"esperando": "submenu_buro"}
            return (
                "El Buró de Crédito no es un enemigo, es solo un registro de cómo has manejado tus créditos. Y sí, puede ayudarte o perjudicarte según tu comportamiento.\n"
                "________________________________________\n"
                "📊 ¿Qué es el Buró de Crédito?\n"
                "Es una empresa que guarda tu historial de pagos.\n"
                "📌 Si pagas bien, tu historial será positivo.\n"
                "📌 Si te atrasas, se reflejará ahí.\n"
                "________________________________________\n"
                "💡 Tener historial no es malo.\n"
                "De hecho, si nunca has pedido un crédito, no aparecerás en Buró y eso puede dificultar que te aprueben uno.\n"
                "________________________________________\n"
                "📈 Tu comportamiento crea un “score” o puntaje.\n"
                "• Pagar a tiempo te ayuda\n"
                "• Deber mucho o atrasarte te baja el score\n"
                "• Tener muchas tarjetas al tope también afecta\n"
                "________________________________________\n"
                "❗ Cuidado con estas ideas falsas:\n"
                "• “Estoy en Buró” no siempre es malo\n"
                "• No es una lista negra\n"
                "• No te borran tan fácil (los registros duran años)\n"
                "________________________________________\n"
                "¿Te gustaría saber cómo mejorar tu historial crediticio o qué pasos tomar para subir tu puntaje?\n"
                "Responde *sí* o *no*."
            )

    # ===========================
    # LÓGICA DE ESTADOS (subflujos)
    # ===========================
    if numero in estado_usuario and "esperando" in estado_usuario[numero]:
        contexto = estado_usuario[numero]

        # --- Retroalimentación (👍/👎) después del resultado de una calculadora ---
        if contexto["esperando"] == "feedback_resultado":
            calculadora = contexto.get("feedback_calculadora", "desconocida")
            estado_regreso = contexto.get("feedback_estado_regreso")
            mensaje_regreso = contexto.get("feedback_mensaje_regreso")
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio in ["👍", "pulgar arriba", "si", "sí", "1", "util", "útil"]:
                _registrar_evento_uso(numero, f"feedback_pendiente:{calculadora}", f"feedback_positivo:{calculadora}")
                estado_usuario[numero] = {"esperando": estado_regreso} if estado_regreso else {}
                return "¡Qué bueno! Gracias por avisarme 🙌" + (
                    "\n\n" + mensaje_regreso if mensaje_regreso else "\n\nEscribe *menú* para ver todas las opciones."
                )
            if texto_limpio in ["👎", "pulgar abajo", "no", "2", "no util", "no útil"]:
                _registrar_evento_uso(numero, f"feedback_pendiente:{calculadora}", f"feedback_negativo:{calculadora}")
                estado_usuario[numero] = {"esperando": estado_regreso} if estado_regreso else {}
                return "Gracias por decírmelo, nos ayuda a mejorar 🙏" + (
                    "\n\n" + mensaje_regreso if mensaje_regreso else "\n\nEscribe *menú* para ver todas las opciones."
                )
            print(f"⚠️ Respuesta de feedback no reconocida. mensaje={mensaje!r} texto_limpio={texto_limpio!r}")
            return "Por favor, responde solo con 👍 o 👎 (o escribe *menú* para salir)."

        # --- Submenú: Ahorro ---
        if contexto["esperando"] == "menu_ahorro":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio in [
                "1", "cuánto debo apartar para lograr mi meta de ahorro",
                "cuanto debo apartar para lograr mi meta de ahorro",
            ]:
                contexto["esperando"] = "ahorro_meta"
                return (
                    "🎯 Vamos a calcular cuánto necesitas apartar para lograr tu meta.\n\n"
                    "1️⃣ ¿Cuánto dinero quieres tener ahorrado en total? (por ejemplo: 15000)"
                )
            if texto_limpio in [
                "2", "consejos para ahorrar sin sufrir en el intento",
                "consejos para ahorrar",
            ]:
                return mensaje_ahorro_consejos
            if texto_limpio in [
                "3", "dónde puedo comparar cuentas de ahorro entre bancos",
                "donde puedo comparar cuentas de ahorro entre bancos",
                "comparar cuentas de ahorro",
            ]:
                return mensaje_ahorro_comparar_cuentas
            if texto_limpio in [
                "4", "calculadora de presupuesto", "regla 50/30/20", "presupuesto",
            ]:
                contexto["esperando"] = "presupuesto_ingreso"
                return mensaje_intro_presupuesto
            return "Por favor, elige una opción válida del menú de Ahorro, o escribe *menú* para regresar al inicio."

        # --- Submenú: Inversión ---
        if contexto["esperando"] == "menu_inversion":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio in [
                "1", "cuánto puede crecer mi dinero si invierto",
                "cuanto puede crecer mi dinero si invierto",
            ]:
                contexto["esperando"] = "inversion_monto_inicial"
                return (
                    "📈 Vamos a calcular cuánto puede crecer tu dinero.\n\n"
                    "1️⃣ ¿Con cuánto dinero vas a empezar a invertir? Si vas a empezar desde cero, "
                    "escribe 0. (por ejemplo: 5000)"
                )
            if texto_limpio in [
                "2", "conceptos básicos antes de invertir",
                "conceptos basicos antes de invertir",
            ]:
                return mensaje_inversion_conceptos_basicos
            if texto_limpio in [
                "3", "cetes y cetesdirecto: invertir con bajo riesgo",
                "cetes y cetesdirecto", "cetes", "cetesdirecto",
            ]:
                return mensaje_inversion_cetes
            if texto_limpio in [
                "4", "cómo identificar fraudes de inversión",
                "como identificar fraudes de inversión",
                "como identificar fraudes de inversion",
            ]:
                return mensaje_inversion_fraudes
            return "Por favor, elige una opción válida del menú de Inversión, o escribe *menú* para regresar al inicio."

        # --- Submenú: Jubilación ---
        if contexto["esperando"] == "menu_jubilacion":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio in ["1", "calculadoras de jubilación", "calculadoras de jubilacion"]:
                contexto["esperando"] = "menu_jubilacion_calculadoras"
                return mensaje_calculadoras_jubilacion
            if texto_limpio in [
                "2", "qué es una afore y cómo saber en cuál estoy",
                "que es una afore y como saber en cual estoy",
            ]:
                return mensaje_jubilacion_afore
            if texto_limpio in [
                "3", "cómo se calcula mi pensión? ley 73 vs. ley 97",
                "como se calcula mi pension ley 73 vs ley 97",
                "ley 73", "ley 97", "ley 73 vs ley 97",
            ]:
                return mensaje_jubilacion_ley73_vs_ley97
            if texto_limpio in [
                "4", "aportaciones voluntarias: cómo aumentar tu ahorro para el retiro",
                "aportaciones voluntarias",
            ]:
                return mensaje_jubilacion_aportaciones_voluntarias
            if texto_limpio in [
                "5", "qué pasa si cambio de trabajo o dejo de cotizar",
                "que pasa si cambio de trabajo o dejo de cotizar",
            ]:
                return mensaje_jubilacion_cambio_trabajo
            if texto_limpio in [
                "6", "no he trabajado de forma formal ¿aún así puedo ahorrar para mi retiro",
                "no he trabajado de forma formal, ¿aún así puedo ahorrar para mi retiro?",
                "no he trabajado de forma formal aun asi puedo ahorrar para mi retiro",
                "trabajador independiente",
            ]:
                return mensaje_jubilacion_independiente
            return "Por favor, elige una opción válida del menú de Jubilación, o escribe *menú* para regresar al inicio."

        # --- Submenú: Calculadoras de jubilación ---
        if contexto["esperando"] == "menu_jubilacion_calculadoras":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio in ["1", "imss", "trabajadores que cotizan al imss"]:
                contexto["esperando"] = "imss_saldo_actual"
                return (
                    "🌅 Vamos a estimar tu saldo y tu pensión con una versión simplificada de la metodología "
                    "oficial de CONSAR para trabajadores que cotizan al IMSS (Régimen de Ley 97).\n\n"
                    "1️⃣ ¿Cuál es el saldo actual de tu cuenta individual? Es la suma de las subcuentas de "
                    "Retiro, Cesantía en edad avanzada y Vejez (RCV), y Ahorro Voluntario (sin contar SAR-92 "
                    "ni Vivienda). Lo puedes consultar en la app de tu Afore o en la app del SAR (CONSAR). Si "
                    "no lo sabes o vas a empezar desde cero, escribe 0. (ejemplo: 45000)"
                )
            if texto_limpio in ["2", "ley 73", "ley73", "trabajadores que cotizaban al imss antes de julio de 1997"]:
                contexto["esperando"] = "ley73_salario_promedio"
                return (
                    "🌅 Vamos a estimar tu pensión con una versión simplificada de la metodología que usan "
                    "las Afores para el régimen de Ley 73 (para quienes cotizaron al IMSS antes del 1 de "
                    "julio de 1997).\n\n"
                    "1️⃣ ¿Cuál es tu salario mensual promedio de las últimas 250 semanas (casi 5 años) que "
                    "cotizaste? Lo puedes consultar en la app del IMSS Digital. Este dato no se guarda ni se "
                    "comparte con nadie (ni con el SAT ni con nadie más): solo se usa aquí, en este momento, "
                    "para hacer el cálculo. (ejemplo: 15000)"
                )
            if texto_limpio in ["3", "issste", "isste", "trabajadores que cotizan al issste"]:
                contexto["esperando"] = "issste_saldo_actual"
                return (
                    "🌅 Vamos a estimar tu saldo y tu pensión con una versión simplificada de la metodología "
                    "oficial de CONSAR para trabajadores que cotizan al ISSSTE (Régimen de cuentas "
                    "individuales).\n\n"
                    "1️⃣ ¿Cuál es el saldo actual de tu cuenta individual? Es la suma de las subcuentas de "
                    "Retiro, Cesantía en edad avanzada y Vejez (RCV), y Ahorro Voluntario (sin contar SAR-92 "
                    "ni Vivienda). Lo puedes consultar en la app de tu Afore o en la app del SAR (CONSAR). Si "
                    "no lo sabes o vas a empezar desde cero, escribe 0. (ejemplo: 45000)"
                )
            if texto_limpio in ["4", "independientes", "trabajadores independientes"]:
                contexto["esperando"] = "jubilacion_saldo_actual"
                return (
                    "🌅 Vamos a estimar tu ahorro para el retiro con la metodología oficial de CONSAR para "
                    "trabajadores independientes.\n\n"
                    "1️⃣ ¿Cuál es el saldo actual de tu cuenta individual? Es la suma de las subcuentas de "
                    "Retiro, Cesantía en edad avanzada y Vejez (RCV), y Ahorro Voluntario (sin contar SAR-92 "
                    "ni Vivienda). Lo puedes consultar en la app de tu Afore o en la app del SAR (CONSAR). Si "
                    "no lo sabes o vas a empezar desde cero, escribe 0. (ejemplo: 45000)"
                )
            if texto_limpio in ["5", "tutorial", "tutorial para el uso de las calculadoras"]:
                return mensaje_jubilacion_tutorial
            return "Por favor, elige una opción válida (1 a 5), o escribe *menú* para regresar al inicio."

        # --- Submenú: Evalúa tu salud financiera ---
        if contexto["esperando"] == "menu_salud":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            mapa_opciones = {
                "1": ["resiliencia"],
                "2": ["libertad"],
                "3": ["seguridad"],
                "4": ["control"],
                "5": ORDEN_DIMENSIONES_SALUD,
            }
            dimensiones_elegidas = mapa_opciones.get(texto_limpio)
            if dimensiones_elegidas is None:
                return "Por favor, elige una opción del 1 al 5, o escribe *menú* para regresar al inicio."
            estado_usuario[numero] = {
                "esperando": "salud_pregunta",
                "salud_dimensiones": dimensiones_elegidas,
                "salud_dim_idx": 0,
                "salud_preg_idx": 0,
                "salud_puntajes": {},
            }
            primera_dim = DIMENSIONES_SALUD[dimensiones_elegidas[0]]
            return (
                "Vamos a empezar. Responde con la mayor honestidad posible; no hay respuestas correctas o "
                "incorrectas, solo te ayudan a entender mejor tu situación 🙂\n\n"
                + _formatear_pregunta_salud(primera_dim, 0, primera=True)
            )

        # --- Evalúa tu salud financiera: flujo de preguntas ---
        if contexto["esperando"] == "salud_pregunta":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio not in ["1", "2", "3", "4", "5"]:
                return "Por favor responde con un número del 1 (completamente en desacuerdo) al 5 (completamente de acuerdo)."

            valor = int(texto_limpio)
            dim_key = contexto["salud_dimensiones"][contexto["salud_dim_idx"]]
            pregunta_idx_actual = contexto["salud_preg_idx"]
            # Registramos cada respuesta de la encuesta como su propio evento
            # (con la dimensión, el número de pregunta y el valor elegido, sin
            # texto libre), porque el "esperando" no cambia pregunta a pregunta
            # y el registro genérico de arriba no alcanzaría a verlas.
            _registrar_evento_uso(
                numero,
                f"salud_pregunta:{dim_key}:{pregunta_idx_actual}",
                f"salud_respuesta:{dim_key}:{pregunta_idx_actual}:valor={valor}",
            )
            contexto["salud_puntajes"][dim_key] = contexto["salud_puntajes"].get(dim_key, 0) + valor
            contexto["salud_preg_idx"] += 1

            resultado_texto = ""
            dim_actual = DIMENSIONES_SALUD[dim_key]
            if contexto["salud_preg_idx"] >= len(dim_actual["preguntas"]):
                # Se completó esta dimensión: calculamos y mostramos su resultado.
                puntaje_dim = contexto["salud_puntajes"][dim_key]
                _registrar_evento_uso(
                    numero,
                    f"salud_dimension_completada:{dim_key}",
                    f"salud_puntaje:{dim_key}:{puntaje_dim}",
                )
                resultado_texto = _resultado_dimension_salud(dim_key, puntaje_dim) + "\n\n"
                contexto["salud_dim_idx"] += 1
                contexto["salud_preg_idx"] = 0

                if contexto["salud_dim_idx"] >= len(contexto["salud_dimensiones"]):
                    # No quedan más dimensiones por evaluar: terminamos aquí.
                    estado_usuario[numero] = {"esperando": "menu_salud"}
                    return resultado_texto + mensaje_salud_cierre

            siguiente_dim_key = contexto["salud_dimensiones"][contexto["salud_dim_idx"]]
            siguiente_dim = DIMENSIONES_SALUD[siguiente_dim_key]
            idx = contexto["salud_preg_idx"]
            pregunta_texto = _formatear_pregunta_salud(siguiente_dim, idx, primera=(idx == 0))
            return resultado_texto + pregunta_texto

        # --- Submenú: Género y finanzas ---
        if contexto["esperando"] == "menu_genero":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio in [
                "1", "la brecha de género en el ahorro para el retiro",
                "la brecha de genero en el ahorro para el retiro",
            ]:
                return mensaje_genero_brecha_retiro
            if texto_limpio in [
                "2", "qué es la violencia económica y patrimonial",
                "que es la violencia economica y patrimonial",
            ]:
                return mensaje_genero_violencia_economica
            return "Por favor, elige una opción válida de esta sección, o escribe *menú* para regresar al inicio."

        # --- Submenú: Herramientas para el emprendedor ---
        if contexto["esperando"] == "menu_emprendedor":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio == "1":
                estado_usuario[numero] = {"esperando": "emprendedor_unidades"}
                return (
                    "🧰 Vamos a calcular a cuánto te conviene vender tu producto o servicio, tu punto de "
                    "equilibrio y la utilidad que tendrías. Puedes usar cualquier periodo para tu análisis "
                    "(por ejemplo, por semana, por mes o por temporada); solo asegúrate de que todos los "
                    "datos que me des sean para ese MISMO periodo.\n\n"
                    "1️⃣ ¿Cuántas unidades (productos o servicios) esperas vender en ese periodo? Escribe "
                    "solo el número. (ejemplo: 100)"
                )
            if texto_limpio == "2":
                estado_usuario[numero] = {"esperando": "empc_frecuencia"}
                return (
                    "🧰 Esta es la calculadora completa: vamos a incluir también costos variables en %, "
                    "depreciación, un posible crédito del negocio, e impuestos. Son varias preguntas, pero "
                    "el resultado es más preciso.\n\n" + MENSAJE_FRECUENCIA_EMPRENDEDOR
                )
            if texto_limpio in [
                "3", "calculadora para negocios turísticos", "calculadora para negocios turisticos",
                "negocios turísticos", "negocios turisticos", "turismo",
            ]:
                estado_usuario[numero] = {"esperando": "turismo_capacidad"}
                return mensaje_turismo_intro
            if texto_limpio in ["4", "tips financieros para tu negocio", "tips financieros"]:
                return mensaje_emprendedor_tips + "\n" + mensaje_submenu_emprendedor
            return "Por favor, elige una opción válida de esta sección, o escribe *menú* para regresar al inicio."

        # --- Herramientas para el emprendedor: flujo de precio y punto de equilibrio ---
        if contexto["esperando"] == "emprendedor_unidades":
            try:
                unidades = Decimal(mensaje.replace(",", ""))
                if unidades <= 0:
                    return "Las unidades deben ser mayores a cero. ¿Cuántas unidades esperas vender en ese periodo? (ejemplo: 100)"
                contexto["emprendedor_unidades"] = unidades
                contexto["esperando"] = "emprendedor_costo_unitario"
                return "2️⃣ ¿Cuánto te cuesta producir o comprar cada unidad? (ejemplo: 50)"
            except:
                return "Por favor, indica las unidades como un número (ejemplo: 100)."

        if contexto["esperando"] == "emprendedor_costo_unitario":
            try:
                costo_unitario = Decimal(mensaje.replace(",", ""))
                if costo_unitario < 0:
                    return "Ese número no puede ser negativo 🙂 ¿Cuánto te cuesta producir o comprar cada unidad?"
                contexto["emprendedor_costo_unitario"] = costo_unitario
                contexto["esperando"] = "emprendedor_costos_fijos"
                return (
                    "3️⃣ ¿Cuánto gastas en total en costos fijos en ese mismo periodo? Por ejemplo, renta, "
                    "sueldos, luz, internet (sin contar lo que gastas por cada unidad que vendes). Si tienes "
                    "un préstamo relacionado a tu negocio, puedes incluir aquí el pago que hagas en ese "
                    "periodo. (ejemplo: 8000)"
                )
            except:
                return "Por favor, indica el costo por unidad como un número (ejemplo: 50)."

        if contexto["esperando"] == "emprendedor_costos_fijos":
            try:
                costos_fijos = Decimal(mensaje.replace(",", ""))
                if costos_fijos < 0:
                    return "Ese número no puede ser negativo 🙂 ¿Cuánto gastas en total en costos fijos en ese mismo periodo?"
                contexto["emprendedor_costos_fijos"] = costos_fijos
                contexto["esperando"] = "emprendedor_utilidad_deseada"
                return (
                    "4️⃣ ¿Cuánto te gustaría ganar de utilidad (ganancia) en ese mismo periodo, además de "
                    "cubrir tus costos? (ejemplo: 5000)"
                )
            except:
                return "Por favor, indica tus costos fijos como un número (ejemplo: 8000)."

        if contexto["esperando"] == "emprendedor_utilidad_deseada":
            try:
                utilidad_deseada = Decimal(mensaje.replace(",", ""))
                if utilidad_deseada < 0:
                    return "Ese número no puede ser negativo 🙂 ¿Cuánto te gustaría ganar de utilidad en ese mismo periodo?"
                contexto["emprendedor_utilidad_deseada"] = utilidad_deseada
                precio_sugerido = calcular_precio_sugerido_emprendedor(
                    contexto["emprendedor_unidades"],
                    contexto["emprendedor_costo_unitario"],
                    contexto["emprendedor_costos_fijos"],
                    utilidad_deseada,
                )
                contexto["emprendedor_precio_sugerido"] = precio_sugerido
                contexto["esperando"] = "emprendedor_precio_prueba"
                return (
                    f"💲 Con esos datos, para ganar ${utilidad_deseada:,.2f} vendiendo "
                    f"{contexto['emprendedor_unidades']:,.0f} unidades, necesitarías vender cada una en "
                    f"aproximadamente *${precio_sugerido:,.2f}*.\n\n"
                    "¿A qué precio tienes pensado vender realmente? Puedes usar este mismo precio sugerido "
                    f"(escribe {precio_sugerido:,.2f}) o probar otro número, para ver tu punto de equilibrio "
                    "y la utilidad que tendrías."
                )
            except:
                return "Por favor, indica la utilidad deseada como un número (ejemplo: 5000)."

        if contexto["esperando"] == "emprendedor_precio_prueba":
            try:
                precio_prueba = Decimal(mensaje.replace(",", "").replace("$", ""))
                if precio_prueba <= 0:
                    return "El precio debe ser mayor a cero. ¿A qué precio tienes pensado vender cada unidad?"
                costo_unitario = contexto["emprendedor_costo_unitario"]
                if precio_prueba <= costo_unitario:
                    return (
                        f"⚠️ A ${precio_prueba:,.2f} por unidad, no alcanzas ni a cubrir tu costo por unidad "
                        f"(${costo_unitario:,.2f}), así que entre más vendas, más perderías. Prueba con un "
                        f"precio mayor a ${costo_unitario:,.2f}."
                    )
                resultado = calcular_resultado_precio_emprendedor(
                    contexto["emprendedor_unidades"],
                    costo_unitario,
                    contexto["emprendedor_costos_fijos"],
                    precio_prueba,
                )
                return _con_feedback(numero, "emprendedor_simple", resultado, "menu_emprendedor", mensaje_submenu_emprendedor)
            except:
                return "Por favor, indica el precio como un número (ejemplo: 60)."

        # --- Herramientas para el emprendedor: calculadora completa ---
        if contexto["esperando"] == "empc_frecuencia":
            if texto_limpio == "5":
                contexto["esperando"] = "empc_frecuencia_otro"
                return (
                    "¿Cuántas veces al año se repite ese periodo? (ejemplo: si vas a analizar tu negocio "
                    "cada 10 días, serían 36 veces al año)"
                )
            if texto_limpio not in FRECUENCIAS_PAGO:
                return MENSAJE_FRECUENCIA_EMPRENDEDOR
            frecuencia_label, periodos_por_anio = FRECUENCIAS_PAGO[texto_limpio]
            contexto["empc_frecuencia_label"] = frecuencia_label
            contexto["empc_periodos_por_anio"] = periodos_por_anio
            contexto["empc_frecuencia_frase"] = FRECUENCIA_EMPRENDEDOR_FRASE[frecuencia_label]
            contexto["esperando"] = "empc_unidades"
            return (
                f"Perfecto, vamos a trabajar con un periodo {frecuencia_label}. Todos los datos que te voy "
                "a pedir (ventas, costos y, si tienes uno, el pago de tu crédito) van a ser para ese mismo "
                "periodo.\n\n"
                f"1️⃣ ¿Cuántas unidades (productos o servicios) esperas vender {contexto['empc_frecuencia_frase']}? "
                "Escribe solo el número. (ejemplo: 100)"
            )

        if contexto["esperando"] == "empc_frecuencia_otro":
            try:
                periodos_por_anio = Decimal(mensaje.replace(",", ""))
                if periodos_por_anio <= 0:
                    return "Ese número debe ser mayor a cero. ¿Cuántas veces al año se repite tu periodo?"
                contexto["empc_frecuencia_label"] = "personalizada"
                contexto["empc_periodos_por_anio"] = periodos_por_anio
                contexto["empc_frecuencia_frase"] = FRECUENCIA_EMPRENDEDOR_FRASE["personalizada"]
                contexto["esperando"] = "empc_unidades"
                return (
                    "Perfecto, vamos a trabajar con ese periodo. Todos los datos que te voy a pedir (ventas, "
                    "costos y, si tienes uno, el pago de tu crédito) van a ser para ese mismo periodo.\n\n"
                    "1️⃣ ¿Cuántas unidades (productos o servicios) esperas vender en tu periodo elegido? "
                    "Escribe solo el número. (ejemplo: 100)"
                )
            except:
                return "Por favor, indica un número (ejemplo: 36)."

        if contexto["esperando"] == "empc_unidades":
            try:
                unidades = Decimal(mensaje.replace(",", ""))
                if unidades <= 0:
                    frase = contexto.get("empc_frecuencia_frase", "en tu periodo elegido")
                    return f"Las unidades deben ser mayores a cero. ¿Cuántas unidades esperas vender {frase}? (ejemplo: 100)"
                contexto["empc_unidades"] = unidades
                contexto["esperando"] = "empc_costo_unitario"
                return "2️⃣ ¿Cuánto te cuesta producir o comprar cada unidad? (ejemplo: 50)"
            except:
                return "Por favor, indica las unidades como un número (ejemplo: 100)."

        if contexto["esperando"] == "empc_costo_unitario":
            try:
                costo_unitario = Decimal(mensaje.replace(",", ""))
                if costo_unitario < 0:
                    return "Ese número no puede ser negativo 🙂 ¿Cuánto te cuesta producir o comprar cada unidad?"
                contexto["empc_costo_unitario"] = costo_unitario
                contexto["esperando"] = "empc_pct_variable"
                return (
                    "3️⃣ ¿Tienes algún costo que se calcule como % de tus ventas (no por unidad)? Por "
                    "ejemplo, una comisión bancaria por cobrar con tarjeta, la comisión de una plataforma "
                    "donde vendes, o una cuota de un colectivo o bazar. Si no aplica, escribe 0. "
                    "(ejemplo: 3, si es 3%)"
                )
            except:
                return "Por favor, indica el costo por unidad como un número (ejemplo: 50)."

        if contexto["esperando"] == "empc_pct_variable":
            try:
                pct_variable = Decimal(mensaje.replace(",", "").replace("%", ""))
                if pct_variable < 0 or pct_variable >= 100:
                    return "Ese porcentaje debe estar entre 0 y menos de 100. Si no aplica, escribe 0."
                contexto["empc_pct_variable"] = pct_variable
                contexto["esperando"] = "empc_costos_fijos"
                frase = contexto.get("empc_frecuencia_frase", "en tu periodo elegido")
                return (
                    f"4️⃣ ¿Cuánto gastas en total en costos fijos {frase}? Por ejemplo, "
                    "renta, sueldos, luz, internet (sin contar la depreciación ni el pago de un crédito, que "
                    "te voy a preguntar aparte). (ejemplo: 8000)"
                )
            except:
                return "Por favor, indica el porcentaje como un número (ejemplo: 3, o 0 si no aplica)."

        if contexto["esperando"] == "empc_costos_fijos":
            try:
                costos_fijos = Decimal(mensaje.replace(",", ""))
                frase = contexto.get("empc_frecuencia_frase", "en tu periodo elegido")
                if costos_fijos < 0:
                    return f"Ese número no puede ser negativo 🙂 ¿Cuánto gastas en total en costos fijos {frase}?"
                contexto["empc_costos_fijos"] = costos_fijos
                contexto["esperando"] = "empc_depreciacion"
                return (
                    "5️⃣ ¿Tienes depreciación de maquinaria, equipo u otros bienes que uses en tu negocio "
                    f"({frase})? Es el desgaste de esos bienes con el tiempo. Si no tienes o no "
                    "llevas ese control, escribe 0. (ejemplo: 200)"
                )
            except:
                return "Por favor, indica tus costos fijos como un número (ejemplo: 8000)."

        if contexto["esperando"] == "empc_depreciacion":
            try:
                depreciacion = Decimal(mensaje.replace(",", ""))
                if depreciacion < 0:
                    return "Ese número no puede ser negativo 🙂 Si no tienes depreciación que considerar, escribe 0."
                contexto["empc_depreciacion"] = depreciacion
                contexto["esperando"] = "empc_tiene_credito"
                return (
                    "6️⃣ ¿Tienes un crédito o préstamo relacionado con tu negocio?\n"
                    "1️⃣ Sí\n"
                    "2️⃣ No"
                )
            except:
                return "Por favor, indica la depreciación como un número (ejemplo: 200, o 0 si no aplica)."

        if contexto["esperando"] == "empc_tiene_credito":
            if texto_limpio not in ["1", "2", "sí", "si", "no"]:
                return "Por favor, responde 1 (Sí) o 2 (No)."
            if texto_limpio in ["1", "sí", "si"]:
                contexto["esperando"] = "empc_credito_monto"
                frase = contexto.get("empc_frecuencia_frase", "en tu periodo elegido")
                return (
                    f"Como elegiste analizar tu negocio {frase}, el pago de este crédito también se va a "
                    "calcular en ese mismo periodo, para que los números cuadren entre sí.\n\n"
                    "¿Cuál es el monto del crédito? (ejemplo: 30000)"
                )
            contexto["empc_credito_interes"] = Decimal("0")
            contexto["empc_credito_amortizacion"] = Decimal("0")
            contexto["esperando"] = "empc_tasa_impositiva"
            return (
                "7️⃣ ¿Qué porcentaje aproximado de tu utilidad pagas de impuestos? Si no llevas ese control "
                "o no estás seguro/a, puedes escribir 0. (ejemplo: 10)"
            )

        if contexto["esperando"] == "empc_credito_monto":
            try:
                monto_credito = Decimal(mensaje.replace(",", ""))
                if monto_credito <= 0:
                    return "El monto del crédito debe ser mayor a cero. ¿Cuál es el monto del crédito? (ejemplo: 30000)"
                contexto["empc_credito_monto"] = monto_credito
                contexto["esperando"] = "empc_credito_tasa"
                return (
                    "¿Cuál es la tasa de interés ANUAL de ese crédito? (ejemplo: si te dijeron 30% anual, "
                    "solo escribe 30)"
                )
            except:
                return "Por favor, indica el monto del crédito como un número (ejemplo: 30000)."

        if contexto["esperando"] == "empc_credito_tasa":
            try:
                tasa_credito = Decimal(mensaje.replace(",", "").replace("%", ""))
                if tasa_credito < 0:
                    return "La tasa de interés no puede ser negativa. ¿Cuál es la tasa de interés ANUAL de ese crédito?"
                contexto["empc_credito_tasa"] = tasa_credito
                contexto["esperando"] = "empc_credito_plazo"
                return (
                    "¿A cuántos años es el plazo de ese crédito? Puedes usar decimales si no es un número "
                    "exacto de años (ejemplo: 3, o 2.5)"
                )
            except:
                return "Por favor, indica la tasa de interés como un número (ejemplo: 30)."

        if contexto["esperando"] == "empc_credito_plazo":
            try:
                anios_credito = Decimal(mensaje.replace(",", ""))
                if anios_credito <= 0:
                    return "El plazo debe ser mayor a cero. ¿A cuántos años es el plazo de ese crédito? (ejemplo: 3)"
                periodos_por_anio = contexto["empc_periodos_por_anio"]
                plazo_total, tasa_periodo = calcular_plazo_y_tasa_periodo(
                    anios_credito, contexto["empc_credito_tasa"], periodos_por_anio
                )
                interes, amortizacion = calcular_pago_credito_primer_periodo(
                    contexto["empc_credito_monto"], tasa_periodo, plazo_total
                )
                contexto["empc_credito_interes"] = interes
                contexto["empc_credito_amortizacion"] = amortizacion
                contexto["esperando"] = "empc_tasa_impositiva"
                return (
                    f"Con eso, el crédito se pagaría en {plazo_total} pagos, siguiendo el mismo periodo que "
                    "elegiste para tu análisis.\n\n"
                    "7️⃣ ¿Qué porcentaje aproximado de tu utilidad pagas de impuestos? Si no llevas ese "
                    "control o no estás seguro/a, puedes escribir 0. (ejemplo: 10)"
                )
            except:
                return "Por favor, indica el plazo en años como un número (ejemplo: 3)."

        if contexto["esperando"] == "empc_tasa_impositiva":
            try:
                tasa_impositiva = Decimal(mensaje.replace(",", "").replace("%", ""))
                if tasa_impositiva < 0 or tasa_impositiva >= 100:
                    return "Ese porcentaje debe estar entre 0 y menos de 100. Si no aplica, escribe 0."
                contexto["empc_tasa_impositiva"] = tasa_impositiva
                contexto["esperando"] = "empc_utilidad_deseada"
                frase = contexto.get("empc_frecuencia_frase", "en tu periodo elegido")
                return (
                    f"8️⃣ ¿Cuánto te gustaría ganar de utilidad (ganancia) {frase}, además de cubrir "
                    "tus costos? (ejemplo: 5000)"
                )
            except:
                return "Por favor, indica el porcentaje como un número (ejemplo: 10, o 0 si no aplica)."

        if contexto["esperando"] == "empc_utilidad_deseada":
            try:
                utilidad_deseada = Decimal(mensaje.replace(",", ""))
                frase = contexto.get("empc_frecuencia_frase", "en tu periodo elegido")
                if utilidad_deseada < 0:
                    return f"Ese número no puede ser negativo 🙂 ¿Cuánto te gustaría ganar de utilidad {frase}?"
                contexto["empc_utilidad_deseada"] = utilidad_deseada
                precio_sugerido = calcular_precio_sugerido_emprendedor_completo(
                    contexto["empc_unidades"],
                    contexto["empc_costo_unitario"],
                    contexto["empc_pct_variable"],
                    contexto["empc_costos_fijos"],
                    contexto["empc_depreciacion"],
                    contexto["empc_credito_interes"],
                    contexto["empc_credito_amortizacion"],
                    contexto["empc_tasa_impositiva"],
                    utilidad_deseada,
                )
                contexto["empc_precio_sugerido"] = precio_sugerido
                contexto["esperando"] = "empc_precio_prueba"
                return (
                    f"💲 Con esos datos, para ganar ${utilidad_deseada:,.2f} vendiendo "
                    f"{contexto['empc_unidades']:,.0f} unidades {frase}, necesitarías vender cada una en "
                    f"aproximadamente *${precio_sugerido:,.2f}*.\n\n"
                    "¿A qué precio tienes pensado vender realmente? Puedes usar este mismo precio sugerido "
                    f"(escribe {precio_sugerido:,.2f}) o probar otro número, para ver tu punto de equilibrio "
                    "y la utilidad neta que tendrías."
                )
            except:
                return "Por favor, indica la utilidad deseada como un número (ejemplo: 5000)."

        if contexto["esperando"] == "empc_precio_prueba":
            try:
                precio_prueba = Decimal(mensaje.replace(",", "").replace("$", ""))
                if precio_prueba <= 0:
                    return "El precio debe ser mayor a cero. ¿A qué precio tienes pensado vender cada unidad?"
                costo_unitario = contexto["empc_costo_unitario"]
                pct_variable = contexto["empc_pct_variable"]
                margen_prueba = precio_prueba * (Decimal("1") - pct_variable / Decimal("100")) - costo_unitario
                if margen_prueba <= 0:
                    return (
                        f"⚠️ A ${precio_prueba:,.2f} por unidad, no alcanzas ni a cubrir tu costo por unidad "
                        f"más comisiones (${costo_unitario:,.2f} + {pct_variable}% de comisión), así que "
                        "entre más vendas, más perderías. Prueba con un precio más alto."
                    )
                resultado = calcular_resultado_precio_emprendedor_completo(
                    contexto["empc_unidades"],
                    costo_unitario,
                    pct_variable,
                    contexto["empc_costos_fijos"],
                    contexto["empc_depreciacion"],
                    contexto["empc_credito_interes"],
                    contexto["empc_tasa_impositiva"],
                    precio_prueba,
                    contexto.get("empc_frecuencia_frase", "en tu periodo elegido"),
                )
                return _con_feedback(numero, "emprendedor_completo", resultado, "menu_emprendedor", mensaje_submenu_emprendedor)
            except:
                return "Por favor, indica el precio como un número (ejemplo: 60)."

        # --- Herramientas para el emprendedor: calculadora para negocios turísticos ---
        if contexto["esperando"] == "turismo_capacidad":
            try:
                capacidad = Decimal(mensaje.replace(",", ""))
                if capacidad <= 0:
                    return "La capacidad debe ser mayor a cero. ¿Cuál es la capacidad máxima de tu tour, cuarto o servicio? (ejemplo: 12)"
                contexto["turismo_capacidad"] = capacidad
                contexto["esperando"] = "turismo_ocupacion"
                return (
                    "2️⃣ ¿Qué porcentaje de esa capacidad esperas ocupar EN PROMEDIO, considerando temporada "
                    "alta y baja? Si no estás segura/o, un punto de partida común es 50-60%. (ejemplo: 60)"
                )
            except:
                return "Por favor, indica la capacidad como un número (ejemplo: 12)."

        if contexto["esperando"] == "turismo_ocupacion":
            try:
                ocupacion = Decimal(mensaje.replace(",", "").replace("%", ""))
                if ocupacion <= 0 or ocupacion > 100:
                    return "El porcentaje de ocupación debe estar entre 1 y 100. (ejemplo: 60)"
                contexto["turismo_ocupacion"] = ocupacion
                contexto["esperando"] = "turismo_costos_fijos"
                return (
                    "3️⃣ ¿Cuánto gastas en costos fijos por cada salida, tour o noche (guía, transporte, "
                    "permisos, renta), sin importar cuántas personas vayan? (ejemplo: 2000)"
                )
            except:
                return "Por favor, indica el porcentaje como un número (ejemplo: 60)."

        if contexto["esperando"] == "turismo_costos_fijos":
            try:
                costos_fijos = Decimal(mensaje.replace(",", ""))
                if costos_fijos < 0:
                    return "Ese número no puede ser negativo 🙂 ¿Cuánto gastas en costos fijos por salida?"
                contexto["turismo_costos_fijos"] = costos_fijos
                contexto["esperando"] = "turismo_costo_variable"
                return (
                    "4️⃣ ¿Cuánto gastas por cada persona que participa (comida, entradas, seguro, souvenirs "
                    "incluidos, etc.)? Si no aplica, escribe 0. (ejemplo: 150)"
                )
            except:
                return "Por favor, indica tus costos fijos como un número (ejemplo: 2000)."

        if contexto["esperando"] == "turismo_costo_variable":
            try:
                costo_variable = Decimal(mensaje.replace(",", ""))
                if costo_variable < 0:
                    return "Ese número no puede ser negativo 🙂 Si no aplica, escribe 0."
                contexto["turismo_costo_variable"] = costo_variable
                contexto["esperando"] = "turismo_comision"
                return (
                    "5️⃣ ¿Vendes a través de alguna plataforma o agencia que te cobre comisión (Booking, "
                    "Airbnb, Viator, TripAdvisor, un agente de viajes, etc.)? Si sí, ¿qué porcentaje te "
                    "cobra? Si no, escribe 0. (ejemplo: 20)"
                )
            except:
                return "Por favor, indica tu costo variable por persona como un número (ejemplo: 150, o 0 si no aplica)."

        if contexto["esperando"] == "turismo_comision":
            try:
                comision = Decimal(mensaje.replace(",", "").replace("%", ""))
                if comision < 0 or comision >= 100:
                    return "Ese porcentaje debe estar entre 0 y menos de 100. Si no aplica, escribe 0."
                contexto["turismo_comision"] = comision
                contexto["esperando"] = "turismo_utilidad_deseada"
                return (
                    "6️⃣ ¿Cuánto te gustaría ganar de utilidad por cada salida, tour o noche, además de "
                    "cubrir tus costos? (ejemplo: 1000)"
                )
            except:
                return "Por favor, indica el porcentaje como un número (ejemplo: 20, o 0 si no aplica)."

        if contexto["esperando"] == "turismo_utilidad_deseada":
            try:
                utilidad_deseada = Decimal(mensaje.replace(",", ""))
                if utilidad_deseada < 0:
                    return "Ese número no puede ser negativo 🙂 ¿Cuánto te gustaría ganar de utilidad por salida?"
                contexto["turismo_utilidad_deseada"] = utilidad_deseada
                precio_sugerido, personas_esperadas = calcular_precio_sugerido_turismo(
                    contexto["turismo_capacidad"],
                    contexto["turismo_ocupacion"],
                    contexto["turismo_costos_fijos"],
                    contexto["turismo_costo_variable"],
                    contexto["turismo_comision"],
                    utilidad_deseada,
                )
                contexto["turismo_personas_esperadas"] = personas_esperadas
                contexto["esperando"] = "turismo_precio_prueba"
                return (
                    f"💲 Con esos datos, para ganar ${utilidad_deseada:,.2f} por salida, considerando que en "
                    f"promedio van {personas_esperadas:,.0f} personas ({contexto['turismo_ocupacion']}% de "
                    f"tu capacidad de {contexto['turismo_capacidad']:,.0f}), necesitarías cobrar "
                    f"aproximadamente *${precio_sugerido:,.2f}* por persona.\n\n"
                    "¿A qué precio por persona tienes pensado cobrar realmente? Puedes usar este mismo "
                    f"precio sugerido (escribe {precio_sugerido:,.2f}) o probar otro número, para ver tu "
                    "punto de equilibrio en ocupación y la utilidad que tendrías."
                )
            except:
                return "Por favor, indica la utilidad deseada como un número (ejemplo: 1000)."

        if contexto["esperando"] == "turismo_precio_prueba":
            try:
                precio_prueba = Decimal(mensaje.replace(",", "").replace("$", ""))
                if precio_prueba <= 0:
                    return "El precio debe ser mayor a cero. ¿A qué precio por persona tienes pensado cobrar?"
                resultado, es_viable, margen_persona = calcular_resultado_turismo(
                    contexto["turismo_capacidad"],
                    contexto["turismo_costos_fijos"],
                    contexto["turismo_costo_variable"],
                    contexto["turismo_comision"],
                    contexto["turismo_personas_esperadas"],
                    precio_prueba,
                )
                contexto["turismo_resultado_base"] = resultado
                if not es_viable:
                    return _con_feedback(numero, "emprendedor_turismo", resultado, "menu_emprendedor", mensaje_submenu_emprendedor)

                contexto["turismo_margen_persona"] = margen_persona
                contexto["esperando"] = "turismo_quiere_mensual"
                return (
                    resultado
                    + "\n\n________________________________________\n"
                    "¿Quieres ver esto también a nivel MENSUAL, es decir, cuántos tours necesitas dar al mes "
                    "para cubrir los costos fijos generales de tu negocio (renta de oficina, sueldos fijos, "
                    "seguros, licencias, etc.)? Responde *sí* o *no*."
                )
            except:
                return "Por favor, indica el precio como un número (ejemplo: 60)."

        if contexto["esperando"] == "turismo_quiere_mensual":
            if texto_limpio in ["no", "2"]:
                return _con_feedback(
                    numero, "emprendedor_turismo", contexto["turismo_resultado_base"],
                    "menu_emprendedor", mensaje_submenu_emprendedor,
                )
            if texto_limpio in ["si", "sí", "1"]:
                contexto["esperando"] = "turismo_tours_mensuales"
                return "1️⃣ ¿Cuántos tours o experiencias das (o planeas dar) al mes en promedio? (ejemplo: 12)"
            return "Por favor, responde *sí* o *no*: ¿quieres ver el punto de equilibrio mensual de tu negocio?"

        if contexto["esperando"] == "turismo_tours_mensuales":
            try:
                tours_mensuales = Decimal(mensaje.replace(",", ""))
                if tours_mensuales <= 0:
                    return "Ese número debe ser mayor a cero. ¿Cuántos tours das al mes en promedio?"
                contexto["turismo_tours_mensuales"] = tours_mensuales
                contexto["esperando"] = "turismo_costos_fijos_mensuales"
                return (
                    "2️⃣ Aparte de los costos fijos por salida que ya me diste, ¿tienes otros costos fijos "
                    "MENSUALES del negocio en general (renta de oficina, sueldos fijos, seguros, licencias, "
                    "etc.)? Si no, escribe 0. (ejemplo: 5000)"
                )
            except:
                return "Por favor, indica el número de tours al mes (ejemplo: 12)."

        if contexto["esperando"] == "turismo_costos_fijos_mensuales":
            try:
                costos_fijos_mensuales = Decimal(mensaje.replace(",", ""))
                if costos_fijos_mensuales < 0:
                    return "Ese número no puede ser negativo 🙂 Si no aplica, escribe 0."
                texto_mensual = calcular_punto_equilibrio_mensual_turismo(
                    contexto["turismo_margen_persona"],
                    contexto["turismo_personas_esperadas"],
                    contexto["turismo_capacidad"],
                    contexto["turismo_costos_fijos"],
                    contexto["turismo_tours_mensuales"],
                    costos_fijos_mensuales,
                )
                resultado_final = contexto["turismo_resultado_base"] + texto_mensual
                return _con_feedback(numero, "emprendedor_turismo", resultado_final, "menu_emprendedor", mensaje_submenu_emprendedor)
            except:
                return "Por favor, indica tus costos fijos mensuales del negocio como un número (ejemplo: 5000, o 0 si no aplica)."

        # --- Submenú: Impuestos y cómo afectan tus finanzas ---
        if contexto["esperando"] == "menu_impuestos":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio == "1":
                estado_usuario[numero] = {"esperando": "impuestos_isr_sueldo"}
                return (
                    "1️⃣ ¿Cuál es tu sueldo mensual bruto, antes de cualquier descuento? Escribe solo el "
                    "número. (ejemplo: 15000)"
                )
            if texto_limpio == "2":
                return mensaje_impuestos_devolucion + "\n\n" + mensaje_submenu_impuestos
            if texto_limpio == "3":
                estado_usuario[numero] = {"esperando": "impuestos_resico_ingreso"}
                return mensaje_impuestos_resico_intro
            return "Por favor, elige una opción válida de esta sección, o escribe *menú* para regresar al inicio."

        if contexto["esperando"] == "impuestos_isr_sueldo":
            try:
                sueldo = Decimal(mensaje.replace(",", ""))
                if sueldo <= 0:
                    return "El sueldo debe ser mayor a cero. ¿Cuál es tu sueldo mensual bruto? (ejemplo: 15000)"
                isr, tasa_marginal = calcular_isr_mensual(sueldo)
                tasa_efectiva = ((isr / sueldo) * Decimal("100")).quantize(Decimal("0.01"))
                resultado = (
                    f"📊 Con un sueldo mensual de ${sueldo:,.2f}:\n"
                    f"🧮 ISR estimado antes de otros descuentos: ${isr:,.2f}\n"
                    f"📈 Tu tasa marginal (la de tu último rango) es {tasa_marginal}%\n"
                    f"📉 Tu tasa efectiva real (lo que en verdad pagas sobre TODO tu sueldo) es "
                    f"{tasa_efectiva}%\n\n"
                    "💡 México usa un sistema progresivo: no te cobran ese % sobre todo tu sueldo, solo "
                    "sobre la parte que va cayendo en cada rango. Por eso tu tasa efectiva siempre es "
                    "menor que tu tasa marginal.\n\n"
                    "🔍 *Nota:* Esta es una referencia con la tarifa de ISR vigente en 2026. No incluye el "
                    "\"subsidio para el empleo\" (que puede bajar aún más tu ISR si ganas un sueldo bajo) ni "
                    "otras retenciones como el IMSS, así que tu recibo de nómina real puede variar un poco. "
                    "No sustituye la asesoría de tu área de RH o un contador."
                )
                return _con_feedback(numero, "impuestos_isr", resultado, "menu_impuestos", mensaje_submenu_impuestos)
            except:
                return "Por favor, indica tu sueldo mensual como un número (ejemplo: 15000)."

        if contexto["esperando"] == "impuestos_resico_ingreso":
            try:
                ingreso = Decimal(mensaje.replace(",", ""))
                if ingreso <= 0:
                    return "El monto debe ser mayor a cero. ¿Cuánto facturas o esperas facturar al mes? (ejemplo: 20000)"
                contexto["impuestos_resico_ingreso"] = ingreso
                contexto["esperando"] = "impuestos_resico_gastos"
                return (
                    "2️⃣ Aproximadamente, ¿cuánto gastas al mes en tu negocio (renta, insumos, sueldos, "
                    "etc., que puedas comprobar con factura)? Si no tienes gastos o no sabes, escribe 0."
                )
            except:
                return "Por favor, indica el monto como un número (ejemplo: 20000)."

        if contexto["esperando"] == "impuestos_resico_gastos":
            try:
                gastos = Decimal(mensaje.replace(",", ""))
                if gastos < 0:
                    return "Ese número no puede ser negativo 🙂 Si no tienes gastos que comprobar, escribe 0."
                resultado = calcular_comparacion_resico(contexto["impuestos_resico_ingreso"], gastos)
                return _con_feedback(numero, "impuestos_resico", resultado, "menu_impuestos", mensaje_submenu_impuestos)
            except:
                return "Por favor, indica tus gastos como un número (ejemplo: 5000, o 0 si no tienes)."

        # --- Submenú: Protege tus finanzas (seguros y fraudes) ---
        if contexto["esperando"] == "menu_proteccion":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio == "1":
                return mensaje_proteccion_seguros
            if texto_limpio == "2":
                return mensaje_proteccion_fraudes
            if texto_limpio == "3":
                return mensaje_proteccion_que_hacer
            return "Por favor, elige una opción válida de esta sección, o escribe *menú* para regresar al inicio."

        # --- Submenú: Crédito ---
        if contexto["esperando"] == "menu_credito":
            if texto_limpio in ["menu", "menú"]:
                estado_usuario[numero] = {}
                return saludo_inicial
            if texto_limpio == "1":
                estado_usuario[numero] = {"esperando": "monto_credito"}
                return "Perfecto. Para comenzar, dime el monto del crédito que deseas simular."
            if texto_limpio == "2":
                estado_usuario[numero] = {"esperando": "monto2"}
                return "Para estimar tu ahorro con pagos extra, primero dime el Monto del crédito."
            if texto_limpio == "3":
                estado_usuario[numero] = {"esperando": "precio_contado"}
                return (
                    "Vamos a calcular el costo real de una compra a pagos fijos.\n"
                    "Por favor dime lo siguiente:\n\n"
                    "1️⃣ ¿Cuál es el precio de contado del producto? (ejemplo: 1800)"
                )
            if texto_limpio == "4":
                estado_usuario[numero] = {"esperando": "pago_minimo_saldo"}
                return mensaje_intro_pago_minimo
            if texto_limpio == "5":
                estado_usuario[numero] = {"esperando": "ingreso"}
                return (
                    "Vamos a calcular cuánto podrías solicitar como crédito, según tu capacidad de pago.\n\n"
                    "Primero necesito saber:\n"
                    "1️⃣ ¿Cuál es tu ingreso mensual neto? Es decir, lo que realmente recibes después de "
                    "impuestos: lo que te depositan o te dan en efectivo. (ejemplo: 15000)"
                )
            if texto_limpio == "9":
                contexto["esperando"] = "submenu_buro"
                return (
                    "El Buró de Crédito no es un enemigo, es solo un registro de cómo has manejado tus créditos. Y sí, puede ayudarte o perjudicarte según tu comportamiento.\n"
                    "________________________________________\n"
                    "📊 ¿Qué es el Buró de Crédito?\n"
                    "Es una empresa que guarda tu historial de pagos.\n"
                    "📌 Si pagas bien, tu historial será positivo.\n"
                    "📌 Si te atrasas, se reflejará ahí.\n"
                    "________________________________________\n"
                    "💡 Tener historial no es malo.\n"
                    "De hecho, si nunca has pedido un crédito, no aparecerás en Buró y eso puede dificultar que te aprueben uno.\n"
                    "________________________________________\n"
                    "📈 Tu comportamiento crea un “score” o puntaje.\n"
                    "• Pagar a tiempo te ayuda\n"
                    "• Deber mucho o atrasarte te baja el score\n"
                    "• Tener muchas tarjetas al tope también afecta\n"
                    "________________________________________\n"
                    "❗ Cuidado con estas ideas falsas:\n"
                    "• “Estoy en Buró” no siempre es malo\n"
                    "• No es una lista negra\n"
                    "• No te borran tan fácil (los registros duran años)\n"
                    "________________________________________\n"
                    "¿Te gustaría saber cómo mejorar tu historial crediticio o qué pasos tomar para subir tu puntaje?\n"
                    "Responde *sí* o *no*."
                )
            if texto_limpio == "6":
                return (
                    "🟡 Consejos para pagar un crédito sin ahogarte\n"
                    "Pagar un crédito no tiene que sentirse como una carga eterna. Aquí van algunos consejos sencillos para ayudarte a pagar con más tranquilidad y menos estrés:\n"
                    "________________________________________\n"
                    "✅ 1. Haz pagos anticipados cuando puedas\n"
                    "📌 Aunque no sea obligatorio, abonar un poco más al capital te ahorra intereses y reduce el plazo.\n"
                    "💡 Incluso $200 o $500 adicionales hacen una gran diferencia con el tiempo.\n"
                    "________________________________________\n"
                    "✅ 2. Programa tus pagos en automático\n"
                    "📌 Evitas atrasos, recargos y estrés.\n"
                    "💡 Si no tienes domiciliación, pon recordatorios para no fallar.\n"
                    "________________________________________\n"
                    "✅ 3. Revisa si puedes cambiar tu crédito por uno mejor\n"
                    "📌 A esto se le llama “reestructura” o “portabilidad”.\n"
                    "💡 Si tu historial ha mejorado, podrías conseguir mejores condiciones.\n"
                    "________________________________________\n"
                    "✅ 4. Haz un presupuesto mensual\n"
                    "📌 Saber cuánto entra y cuánto sale te ayuda a organizar tus pagos sin descuidar otras necesidades.\n"
                    "💡 Apóyate en apps, papel o Excel, lo que te funcione.\n"
                    "________________________________________\n"
                    "✅ 5. Prioriza las deudas más caras\n"
                    "📌 Si tienes varias, enfócate primero en las que tienen interés más alto, como tarjetas de crédito.\n"
                    "________________________________________\n"
                ) + "\n" + mensaje_submenu_credito
            if texto_limpio == "7":
                return (
                    "Muchas veces un crédito parece accesible… hasta que ves lo que terminas pagando. Aquí te doy algunas claves para detectar si un crédito es caro:\n\n"
                    "🔍 1. CAT (Costo Anual Total)\n"
                    "Es una medida que incluye la tasa de interés, comisiones y otros cargos.\n"
                    "📌 Entre más alto el CAT, más caro te saldrá el crédito.\n"
                    "💡 Compara el CAT entre diferentes instituciones, no solo la tasa.\n\n"
                    "🔍 2. Comisiones escondidas\n"
                    "Algunos créditos cobran por apertura, por manejo, por pagos tardíos o por pagos anticipados 😵\n"
                    "📌 Lee siempre el contrato antes de firmar.\n\n"
                    "🔍 3. Tasa de interés variable\n"
                    "📌 Algunos créditos no tienen tasa fija, sino que pueden subir.\n"
                    "💡 Revisa si tu tasa es fija o variable. Las variables pueden volverse muy caras si sube la inflación.\n\n"
                    "🔍 4. Pago mensual bajo con plazo largo\n"
                    "Parece atractivo, pero terminas pagando muchísimo más en intereses.\n\n"
                    "❗ Si el crédito parece demasiado fácil o rápido, pero no entiendes bien cuánto vas a pagar en total... ¡es una señal de alerta!\n\n"
                ) + "\n" + mensaje_submenu_credito
            if texto_limpio == "8":
                return (
                    "Solicitar un crédito es una gran responsabilidad. Aquí te comparto algunos errores comunes que muchas personas cometen… ¡y cómo evitarlos!\n"
                    "________________________________________\n"
                    "❌ 1. No saber cuánto terminarás pagando en total\n"
                    "Muchas personas solo se fijan en el pago mensual y no en el costo total del crédito.\n"
                    "✅ Usa simuladores (como el que tengo 😎) para saber cuánto pagarás realmente.\n"
                    "________________________________________\n"
                    "❌ 2. Pedir más dinero del que realmente necesitas\n"
                    "📌 Entre más pidas, más intereses pagas.\n"
                    "✅ Pide solo lo necesario y asegúrate de poder pagarlo.\n"
                    "________________________________________\n"
                    "❌ 3. Aceptar el primer crédito que te ofrecen\n"
                    "📌 Hay diferencias enormes entre una institución y otra.\n"
                    "✅ Compara tasas, comisiones y condiciones antes de decidir.\n"
                    "________________________________________\n"
                    "❌ 4. No leer el contrato completo\n"
                    "Sí, puede ser largo, pero ahí están los detalles importantes:\n"
                    "📌 ¿Hay comisiones por pagar antes de tiempo?\n"
                    "📌 ¿Qué pasa si te atrasas?\n"
                    "✅ Lee con calma o pide que te lo expliquen.\n"
                    "________________________________________\n"
                    "❌ 5. Usar un crédito sin un plan de pago\n"
                    "📌 Si no sabes cómo lo vas a pagar, puedes meterte en problemas.\n"
                    "✅ Haz un presupuesto antes de aceptar cualquier crédito.\n\n"
                ) + "\n" + mensaje_submenu_credito
            if texto_limpio == "10":
                return mensaje_credito_derechos_cobranza
            if texto_limpio == "11":
                estado_usuario[numero] = {"esperando": "deudas_cantidad"}
                return mensaje_intro_plan_deudas
            return "Por favor, elige un número del 1 al 11 del menú de Crédito, o escribe *menú* para regresar al inicio."

        # --- Crédito: pago mínimo de tarjeta ---
        if contexto["esperando"] == "pago_minimo_saldo":
            try:
                saldo = Decimal(mensaje.replace(",", "").replace("$", ""))
                if saldo <= 0:
                    return "El saldo debe ser mayor a cero. ¿Cuál es el saldo actual (deuda) de tu tarjeta?"
                contexto["pago_minimo_saldo"] = saldo
                contexto["esperando"] = "pago_minimo_limite"
                return (
                    "2️⃣ ¿Cuál es el límite de crédito de tu tarjeta? Lo encuentras en tu estado de cuenta o "
                    "en la app de tu banco. (ejemplo: 30000)"
                )
            except:
                return "Por favor, indica el saldo como un número (ejemplo: 18000)."

        if contexto["esperando"] == "pago_minimo_limite":
            try:
                limite = Decimal(mensaje.replace(",", "").replace("$", ""))
                if limite <= 0:
                    return "El límite debe ser mayor a cero. ¿Cuál es el límite de crédito de tu tarjeta?"
                contexto["pago_minimo_limite"] = limite
                contexto["esperando"] = "pago_minimo_tasa"
                return (
                    "3️⃣ ¿Cuál es la tasa de interés ANUAL de tu tarjeta? La encuentras en tu estado de cuenta "
                    "(ejemplo: si dice 45% anual, solo escribe 45).\n\n"
                    "💡 Si no la conoces: según el dato más reciente de Banxico (junio de 2025), en tarjetas "
                    "clásicas ronda 41% anual, y en básicas hasta 56% anual, para quienes no pagan de contado."
                )
            except:
                return "Por favor, indica el límite de crédito como un número (ejemplo: 30000)."

        if contexto["esperando"] == "pago_minimo_tasa":
            try:
                tasa_anual = Decimal(mensaje.replace(",", "").replace("%", ""))
                if tasa_anual < 0:
                    return "La tasa de interés no puede ser negativa. ¿Cuál es la tasa de interés ANUAL de tu tarjeta?"
                saldo = contexto["pago_minimo_saldo"]
                limite = contexto["pago_minimo_limite"]
                pago_minimo, interes, iva, criterio, opcion1, opcion2 = _calcular_pago_minimo_tarjeta(
                    saldo, limite, tasa_anual
                )
                meses, total_pagado, total_interes_iva, se_alcanzo_el_tope = _simular_solo_pago_minimo(
                    saldo, limite, tasa_anual
                )

                texto_desglose = (
                    f"💳 *Tu pago mínimo estimado este mes: ${float(pago_minimo):,.2f}*\n\n"
                    "Se calcula así (regla de Banxico, se cobra la MAYOR de las dos, más intereses e IVA):\n"
                    f"1️⃣ 1.5% de tu saldo (${float(saldo):,.2f}) + intereses + IVA = ${float(opcion1):,.2f}\n"
                    f"2️⃣ 1.25% de tu límite (${float(limite):,.2f}) + intereses + IVA = ${float(opcion2):,.2f}\n"
                    f"👉 Tu banco cobraría la opción {criterio}, por ser la mayor.\n\n"
                    "________________________________________\n"
                )

                if se_alcanzo_el_tope:
                    texto_simulacion = (
                        "😬 *Si SOLO pagaras el mínimo cada mes* (sin volver a usar la tarjeta), no alcanzarías "
                        "a liquidar esta deuda ni en 50 años, porque el pago apenas cubre los intereses y una "
                        "parte muy pequeña del capital.\n\n"
                        "💡 Con estos números, pagar solo el mínimo prácticamente no reduce tu deuda. Necesitas "
                        "abonar más para realmente salir de ella."
                    )
                else:
                    anios = round(meses / 12, 1)
                    texto_simulacion = (
                        "😬 *Si SOLO pagaras el mínimo cada mes* (sin volver a usar la tarjeta):\n"
                        f"📆 Tardarías aproximadamente {meses} meses ({anios} años) en liquidarla.\n"
                        f"💰 En total pagarías ${float(total_pagado):,.2f}, de los cuales ${float(total_interes_iva):,.2f} "
                        "son solo intereses e IVA.\n\n"
                        "💡 Cada peso que abones de más al mínimo reduce ese tiempo y ese costo de forma importante."
                    )

                resultado = (
                    texto_desglose
                    + texto_simulacion
                    + "\n\nSi quieres ver cuánto te ahorrarías agregando una cantidad fija cada mes, prueba la "
                    "opción 2️⃣ Ahorro con pagos extra a un crédito del menú de Crédito.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
                return _con_feedback(numero, "pago_minimo_tarjeta", resultado)
            except Exception as e:
                print(f"Error al calcular pago mínimo: {e}")
                return "Uy, algo no cuadró con esos datos 🤔 Revisa que hayas escrito solo números y vuelve a intentarlo, o escribe *menú* para empezar de nuevo."

        # --- Crédito: plan para pagar varias deudas (bola de nieve / avalancha) ---
        if contexto["esperando"] == "deudas_cantidad":
            try:
                cantidad = int(mensaje.strip())
                if cantidad < 2 or cantidad > 6:
                    return "Por favor, dame un número entre 2 y 6 deudas. ¿Cuántas deudas quieres incluir?"
                contexto["deudas_total"] = cantidad
                contexto["deudas_idx"] = 0
                contexto["deudas_lista"] = []
                contexto["esperando"] = "deudas_saldo"
                return (
                    f"Perfecto, vamos a capturar tus {cantidad} deudas una por una.\n\n"
                    "📋 *Deuda 1*\n"
                    "1️⃣ ¿Cuál es el saldo actual (lo que debes) de esta deuda? (ejemplo: 12000)"
                )
            except:
                return "Por favor, indica cuántas deudas quieres incluir con un número del 2 al 6."

        if contexto["esperando"] == "deudas_saldo":
            try:
                saldo = Decimal(mensaje.replace(",", "").replace("$", ""))
                if saldo <= 0:
                    return "El saldo debe ser mayor a cero. ¿Cuál es el saldo actual de esta deuda?"
                contexto["deudas_actual"] = {"saldo": saldo}
                contexto["esperando"] = "deudas_tasa"
                return "2️⃣ ¿Cuál es la tasa de interés ANUAL de esta deuda? (ejemplo: si es 45% anual, escribe 45)"
            except:
                return "Por favor, indica el saldo como un número (ejemplo: 12000)."

        if contexto["esperando"] == "deudas_tasa":
            try:
                tasa = Decimal(mensaje.replace(",", "").replace("%", ""))
                if tasa < 0:
                    return "La tasa de interés no puede ser negativa. ¿Cuál es la tasa de interés ANUAL de esta deuda?"
                contexto["deudas_actual"]["tasa_anual"] = tasa
                contexto["esperando"] = "deudas_pago"
                return "3️⃣ ¿Cuánto pagas de mínimo cada mes por esta deuda? (ejemplo: 800)"
            except:
                return "Por favor, indica la tasa de interés como un número (ejemplo: 45)."

        if contexto["esperando"] == "deudas_pago":
            try:
                pago_minimo = Decimal(mensaje.replace(",", "").replace("$", ""))
                if pago_minimo <= 0:
                    return "El pago mínimo debe ser mayor a cero. ¿Cuánto pagas de mínimo cada mes por esta deuda?"
                contexto["deudas_actual"]["pago_minimo"] = pago_minimo
                contexto["deudas_lista"].append(contexto["deudas_actual"])
                contexto["deudas_actual"] = {}
                contexto["deudas_idx"] += 1

                if contexto["deudas_idx"] < contexto["deudas_total"]:
                    contexto["esperando"] = "deudas_saldo"
                    return (
                        f"📋 *Deuda {contexto['deudas_idx'] + 1}*\n"
                        "1️⃣ ¿Cuál es el saldo actual (lo que debes) de esta deuda? (ejemplo: 12000)"
                    )

                contexto["esperando"] = "deudas_extra"
                return (
                    "Ya tengo tus deudas. Última pregunta:\n\n"
                    "4️⃣ Además de los mínimos, ¿cuánto dinero EXTRA puedes destinar cada mes a pagar deudas? "
                    "(si no puedes dar nada extra por ahora, escribe 0)"
                )
            except:
                return "Por favor, indica el pago mínimo mensual como un número (ejemplo: 800)."

        if contexto["esperando"] == "deudas_extra":
            try:
                extra_mensual = Decimal(mensaje.replace(",", "").replace("$", ""))
                if extra_mensual < 0:
                    return "El abono extra no puede ser negativo. ¿Cuánto puedes destinar cada mes, además de los mínimos? (si nada, escribe 0)"

                deudas_lista = contexto["deudas_lista"]
                n = len(deudas_lista)

                orden_nieve = sorted(range(n), key=lambda i: deudas_lista[i]["saldo"])
                orden_avalancha = sorted(range(n), key=lambda i: deudas_lista[i]["tasa_anual"], reverse=True)

                meses_n, pagado_n, interes_n, liq_n, tope_n = _simular_estrategia_deudas(
                    deudas_lista, extra_mensual, orden_nieve
                )
                meses_a, pagado_a, interes_a, liq_a, tope_a = _simular_estrategia_deudas(
                    deudas_lista, extra_mensual, orden_avalancha
                )

                lineas_deudas = "\n".join(
                    f"  Deuda {i + 1}: saldo ${float(d['saldo']):,.2f}, tasa {d['tasa_anual']}% anual, "
                    f"mínimo ${float(d['pago_minimo']):,.2f}/mes"
                    for i, d in enumerate(deudas_lista)
                )
                orden_nieve_texto = " → ".join(f"Deuda {i + 1}" for i in orden_nieve)
                orden_avalancha_texto = " → ".join(f"Deuda {i + 1}" for i in orden_avalancha)

                if tope_n or tope_a:
                    resultado = (
                        f"📋 Tus deudas:\n{lineas_deudas}\n\n"
                        "😬 Con estos números (tus mínimos actuales más el abono extra que diste), no alcanzarías "
                        "a liquidar todas tus deudas ni en 50 años con ninguna de las dos estrategias: lo que "
                        "pagas apenas cubre los intereses.\n\n"
                        "💡 Necesitas aumentar el abono extra mensual, o algunos de los mínimos, para realmente "
                        "avanzar. Prueba de nuevo con un abono extra mayor.\n\n"
                        "Escribe *menú* para volver al inicio."
                    )
                    return _con_feedback(numero, "plan_deudas", resultado)

                anios_n = round(meses_n / 12, 1)
                anios_a = round(meses_a / 12, 1)
                diferencia_interes = interes_n - interes_a

                if meses_n == meses_a and interes_n == interes_a:
                    comparacion = (
                        "En tu caso, con solo una deuda o con montos muy parecidos, ambas estrategias te dan "
                        "prácticamente el mismo resultado."
                    )
                elif interes_a <= interes_n:
                    comparacion = (
                        f"🏔️ *La avalancha te ahorra ${float(diferencia_interes):,.2f} en intereses* respecto a la "
                        "bola de nieve, por atacar primero la tasa más cara.\n"
                        "❄️ La bola de nieve puede tardar lo mismo o un poco más, pero como vas liquidando "
                        "primero las deudas más chicas, para muchas personas es más fácil mantener la disciplina "
                        "para seguirla."
                    )
                else:
                    comparacion = (
                        f"❄️ En tu caso, la bola de nieve incluso te ahorra ${float(-diferencia_interes):,.2f} en "
                        "intereses respecto a la avalancha, además de la ventaja de motivación de ir liquidando "
                        "deudas chicas primero."
                    )

                resultado = (
                    f"📋 Tus deudas:\n{lineas_deudas}\n\n"
                    "________________________________________\n"
                    "❄️ *Estrategia bola de nieve* (primero la de menor saldo)\n"
                    f"Orden sugerido: {orden_nieve_texto}\n"
                    f"📆 Quedarías libre de deudas en {meses_n} meses ({anios_n} años).\n"
                    f"💰 Pagarías en total ${float(pagado_n):,.2f}, de los cuales ${float(interes_n):,.2f} son "
                    "intereses.\n"
                    "________________________________________\n"
                    "🏔️ *Estrategia avalancha* (primero la tasa más alta)\n"
                    f"Orden sugerido: {orden_avalancha_texto}\n"
                    f"📆 Quedarías libre de deudas en {meses_a} meses ({anios_a} años).\n"
                    f"💰 Pagarías en total ${float(pagado_a):,.2f}, de los cuales ${float(interes_a):,.2f} son "
                    "intereses.\n"
                    "________________________________________\n"
                    f"{comparacion}\n\n"
                    "💡 En ambos casos, sigue pagando el mínimo de todas tus deudas cada mes, y concentra el "
                    "abono extra en la deuda que va primero en el orden sugerido. Cuando la liquides, usa ese "
                    "dinero (mínimo + extra) para la siguiente de la lista.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
                return _con_feedback(numero, "plan_deudas", resultado)
            except Exception as e:
                print(f"Error al calcular el plan de pago de deudas: {e}")
                return "Uy, algo no cuadró con esos datos 🤔 Revisa que hayas escrito solo números y vuelve a intentarlo, o escribe *menú* para empezar de nuevo."

        # --- Ahorro: calculadora de presupuesto (regla 50/30/20) ---
        if contexto["esperando"] == "presupuesto_ingreso":
            try:
                ingreso = Decimal(mensaje.replace(",", "").replace("$", ""))
                if ingreso <= 0:
                    return "El ingreso debe ser mayor a cero. ¿Cuál es tu ingreso mensual neto?"
                contexto["presupuesto_ingreso"] = ingreso
                necesidades = (ingreso * Decimal("0.50")).quantize(Decimal("0.01"))
                gustos = (ingreso * Decimal("0.30")).quantize(Decimal("0.01"))
                ahorro = (ingreso * Decimal("0.20")).quantize(Decimal("0.01"))
                contexto["esperando"] = "presupuesto_comparar"
                return (
                    f"Con un ingreso mensual de ${float(ingreso):,.2f}, la regla 50/30/20 sugiere:\n\n"
                    f"🏠 Gastos necesarios (50%): ${float(necesidades):,.2f}\n"
                    f"🎉 Gustos (30%): ${float(gustos):,.2f}\n"
                    f"💰 Ahorro o pago de deudas (20%): ${float(ahorro):,.2f}\n\n"
                    "¿Quieres comparar esto con lo que gastas actualmente en cada categoría? Responde *sí* o "
                    "*no*."
                )
            except:
                return "Por favor, indica tu ingreso mensual neto como un número (ejemplo: 12000)."

        if contexto["esperando"] == "presupuesto_comparar":
            if texto_limpio in ["si", "sí", "1"]:
                contexto["esperando"] = "presupuesto_gasto_necesidades"
                return (
                    "Perfecto. Piensa en un mes normal:\n\n"
                    "1️⃣ ¿Cuánto gastas aproximadamente al mes en gastos necesarios (renta o hipoteca, comida, "
                    "transporte, servicios)? (ejemplo: 7000)"
                )
            if texto_limpio in ["no", "2"]:
                ingreso = contexto["presupuesto_ingreso"]
                necesidades = (ingreso * Decimal("0.50")).quantize(Decimal("0.01"))
                gustos = (ingreso * Decimal("0.30")).quantize(Decimal("0.01"))
                ahorro = (ingreso * Decimal("0.20")).quantize(Decimal("0.01"))
                resultado = (
                    f"📊 Resumen de tu presupuesto sugerido (ingreso de ${float(ingreso):,.2f}):\n\n"
                    f"🏠 Gastos necesarios (50%): ${float(necesidades):,.2f}\n"
                    f"🎉 Gustos (30%): ${float(gustos):,.2f}\n"
                    f"💰 Ahorro o pago de deudas (20%): ${float(ahorro):,.2f}\n\n"
                    "💡 No tiene que ser exacto: es un punto de partida para organizar tu dinero. Si quieres, "
                    "en la opción 1️⃣ de *Ahorro* puedo ayudarte a calcular cuánto apartar para una meta "
                    f"específica con esos ${float(ahorro):,.2f} de ahorro.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
                return _con_feedback(numero, "presupuesto", resultado)
            return "Por favor, responde *sí* o *no*: ¿quieres comparar con lo que gastas actualmente?"

        if contexto["esperando"] == "presupuesto_gasto_necesidades":
            try:
                gasto_necesidades = Decimal(mensaje.replace(",", "").replace("$", ""))
                if gasto_necesidades < 0:
                    return "El gasto no puede ser negativo. ¿Cuánto gastas al mes en gastos necesarios?"
                contexto["presupuesto_gasto_necesidades"] = gasto_necesidades
                contexto["esperando"] = "presupuesto_gasto_gustos"
                return (
                    "2️⃣ ¿Cuánto gastas aproximadamente al mes en tus gustos (salidas, streaming, ropa, "
                    "antojos, etc.)? (ejemplo: 2500)"
                )
            except:
                return "Por favor, indica ese gasto como un número (ejemplo: 7000)."

        if contexto["esperando"] == "presupuesto_gasto_gustos":
            try:
                gasto_gustos = Decimal(mensaje.replace(",", "").replace("$", ""))
                if gasto_gustos < 0:
                    return "El gasto no puede ser negativo. ¿Cuánto gastas al mes en tus gustos?"

                ingreso = contexto["presupuesto_ingreso"]
                gasto_necesidades = contexto["presupuesto_gasto_necesidades"]
                necesidades_rec = (ingreso * Decimal("0.50")).quantize(Decimal("0.01"))
                gustos_rec = (ingreso * Decimal("0.30")).quantize(Decimal("0.01"))
                ahorro_rec = (ingreso * Decimal("0.20")).quantize(Decimal("0.01"))
                ahorro_real = (ingreso - gasto_necesidades - gasto_gustos).quantize(Decimal("0.01"))

                pct_necesidades = (gasto_necesidades / ingreso * 100).quantize(Decimal("0.1"))
                pct_gustos = (gasto_gustos / ingreso * 100).quantize(Decimal("0.1"))
                pct_ahorro = (ahorro_real / ingreso * 100).quantize(Decimal("0.1"))

                # Un pequeño margen (5 puntos porcentuales) para no marcar como
                # "fuera de la regla" algo que está prácticamente en el objetivo.
                semaforo_necesidades = "🟢" if gasto_necesidades <= necesidades_rec * Decimal("1.1") else "🔴"
                semaforo_gustos = "🟢" if gasto_gustos <= gustos_rec * Decimal("1.1") else "🔴"
                if ahorro_real < 0:
                    semaforo_ahorro = "🔴"
                elif ahorro_real >= ahorro_rec:
                    semaforo_ahorro = "🟢"
                else:
                    semaforo_ahorro = "🟡"

                # Si el "ahorro" salió negativo (gastó más de lo que ingresa), se
                # muestra con el signo antes del $ (-$2,000.00) en vez de después
                # ($-2,000.00), que se lee raro.
                texto_ahorro_real = (
                    f"-${abs(float(ahorro_real)):,.2f}" if ahorro_real < 0 else f"${float(ahorro_real):,.2f}"
                )
                comparacion = (
                    "📊 *Comparación con la regla 50/30/20*\n\n"
                    f"🏠 Gastos necesarios: gastas ${float(gasto_necesidades):,.2f} ({pct_necesidades}%) vs. "
                    f"${float(necesidades_rec):,.2f} (50%) sugerido {semaforo_necesidades}\n"
                    f"🎉 Gustos: gastas ${float(gasto_gustos):,.2f} ({pct_gustos}%) vs. ${float(gustos_rec):,.2f} "
                    f"(30%) sugerido {semaforo_gustos}\n"
                    f"💰 Te queda para ahorro o deudas: {texto_ahorro_real} ({pct_ahorro}%) vs. "
                    f"${float(ahorro_rec):,.2f} (20%) sugerido {semaforo_ahorro}\n\n"
                )

                if ahorro_real < 0:
                    observacion = (
                        "😬 Según lo que me dijiste, estás gastando más de lo que ingresas cada mes, lo cual "
                        "solo es posible si estás usando ahorros o endeudándote. Si tienes deudas, dentro de "
                        "*Crédito* tengo una calculadora (opción 1️⃣1️⃣) para armar un plan y salir de ellas en "
                        "orden."
                    )
                elif semaforo_necesidades == "🔴":
                    observacion = (
                        "💡 Tus gastos necesarios están bastante por encima del 50% sugerido. Vale la pena "
                        "revisar si hay algo ahí que se pueda reducir (por ejemplo, comparar si hay opciones "
                        "más baratas de transporte o servicios), porque eso es lo que más limita cuánto puedes "
                        "ahorrar."
                    )
                elif semaforo_gustos == "🔴":
                    observacion = (
                        "💡 Tus gustos están por encima del 30% sugerido. No se trata de eliminarlos, sino de "
                        "tenerlos identificados: a veces basta con ponerles un tope mensual."
                    )
                elif semaforo_ahorro == "🟢":
                    observacion = (
                        "🎉 Vas muy bien: estás ahorrando (o pagando deudas) igual o más de lo que sugiere la "
                        "regla. Si quieres, en la opción 1️⃣ de *Ahorro* puedo ayudarte a ponerle una meta "
                        "concreta a ese dinero."
                    )
                else:
                    observacion = (
                        "💡 Vas encaminado/a, aunque el ahorro te quedó un poco por debajo del 20% sugerido. "
                        "No hace falta que sea perfecto desde el primer mes: puedes ir subiendo el porcentaje "
                        "poco a poco."
                    )

                resultado = (
                    comparacion
                    + observacion
                    + "\n\nEscribe *menú* para volver al inicio."
                )
                return _con_feedback(numero, "presupuesto", resultado)
            except Exception as e:
                print(f"Error al calcular el presupuesto: {e}")
                return "Uy, algo no cuadró con esos datos 🤔 Revisa que hayas escrito solo números y vuelve a intentarlo, o escribe *menú* para empezar de nuevo."

        # --- Ahorro: flujo de meta de ahorro ---
        if contexto["esperando"] == "ahorro_meta":
            try:
                contexto["ahorro_meta"] = Decimal(mensaje.replace(",", ""))
                if contexto["ahorro_meta"] <= 0:
                    return "La meta debe ser mayor a cero. ¿Cuánto dinero quieres tener ahorrado en total? (ejemplo: 15000)"
                contexto["esperando"] = "ahorro_inicial"
                return "2️⃣ ¿Ya tienes algo ahorrado hoy para esta meta? Si no tienes nada todavía, escribe 0. (por ejemplo: 2000)"
            except:
                return "Por favor, indica tu meta de ahorro como un número (ejemplo: 15000)."

        if contexto["esperando"] == "ahorro_inicial":
            try:
                contexto["ahorro_inicial"] = Decimal(mensaje.replace(",", ""))
                if contexto["ahorro_inicial"] < 0:
                    return "Ese número no puede ser negativo 🙂 Si no tienes nada ahorrado todavía, escribe 0."
                contexto["esperando"] = "ahorro_tiempo_numero"
                return "3️⃣ ¿En cuánto tiempo quieres lograrlo? Escribe solo el número (por ejemplo: 6)"
            except:
                return "Por favor, escribe solo un número (ejemplo: 2000, o 0 si no tienes nada ahorrado todavía)."

        if contexto["esperando"] == "ahorro_tiempo_numero":
            try:
                tiempo_numero = Decimal(mensaje.replace(",", ""))
                if tiempo_numero <= 0:
                    return "El tiempo debe ser mayor a cero. ¿En cuánto tiempo quieres lograrlo? (ejemplo: 6)"
                contexto["ahorro_tiempo_numero"] = tiempo_numero
                contexto["esperando"] = "ahorro_tiempo_unidad"
                return (
                    "¿Ese número que diste fue en meses o en años?\n"
                    "1️⃣ Meses\n"
                    "2️⃣ Años"
                )
            except:
                return "Por favor, indica el tiempo como un número (ejemplo: 6)."

        if contexto["esperando"] == "ahorro_tiempo_unidad":
            if texto_limpio not in ["1", "2", "meses", "años", "anos", "año", "ano"]:
                return "Por favor, elige 1 (Meses) o 2 (Años)."
            if texto_limpio in ["1", "meses"]:
                meses_totales = contexto["ahorro_tiempo_numero"]
            else:
                meses_totales = contexto["ahorro_tiempo_numero"] * Decimal("12")
            contexto["ahorro_meses_totales"] = meses_totales
            contexto["esperando"] = "ahorro_frecuencia"
            return MENSAJE_FRECUENCIA_AHORRO

        if contexto["esperando"] == "ahorro_frecuencia":
            if texto_limpio == "5":
                contexto["esperando"] = "ahorro_frecuencia_otro"
                return "¿Cuántas veces al año en total apartarías dinero? (ejemplo: 24)"
            if texto_limpio not in FRECUENCIAS_PAGO:
                return "Por favor, elige una opción del 1 al 5."
            try:
                frecuencia_label, periodos_por_anio = FRECUENCIAS_PAGO[texto_limpio]
                resultado = calcular_ahorro_periodico(
                    contexto["ahorro_meta"],
                    contexto["ahorro_inicial"],
                    contexto["ahorro_meses_totales"],
                    periodos_por_anio,
                    frecuencia_label,
                )
                return _con_feedback(numero, "ahorro_meta", resultado)
            except Exception:
                return "Hubo un error al calcular. Revisa tus datos e intenta de nuevo."

        if contexto["esperando"] == "ahorro_frecuencia_otro":
            try:
                periodos_por_anio = Decimal(mensaje.strip())
                if periodos_por_anio <= 0:
                    return "El número de veces al año debe ser mayor a cero (ejemplo: 24)."
                resultado = calcular_ahorro_periodico(
                    contexto["ahorro_meta"],
                    contexto["ahorro_inicial"],
                    contexto["ahorro_meses_totales"],
                    periodos_por_anio,
                    "personalizada",
                )
                return _con_feedback(numero, "ahorro_meta", resultado)
            except Exception:
                return "Por favor, indica un número de veces al año (ejemplo: 24)."

        # --- Inversión: flujo de crecimiento de una inversión ---
        if contexto["esperando"] == "inversion_monto_inicial":
            try:
                monto_inicial = Decimal(mensaje.replace(",", ""))
                if monto_inicial < 0:
                    return "Ese número no puede ser negativo 🙂 Si vas a empezar desde cero, escribe 0."
                contexto["inversion_monto_inicial"] = monto_inicial
                contexto["esperando"] = "inversion_aportacion"
                return (
                    "2️⃣ ¿Cuánto planeas aportar en cada periodo? Si solo vas a invertir el monto "
                    "inicial y nada más, escribe 0. (por ejemplo: 500)"
                )
            except:
                return "Por favor, indica el monto inicial como un número (ejemplo: 5000, o 0 si vas a empezar desde cero)."

        if contexto["esperando"] == "inversion_aportacion":
            try:
                aportacion = Decimal(mensaje.replace(",", ""))
                if aportacion < 0:
                    return "Ese número no puede ser negativo 🙂 Si no vas a aportar más, escribe 0."
                if contexto["inversion_monto_inicial"] == 0 and aportacion == 0:
                    contexto["esperando"] = "inversion_monto_inicial"
                    return (
                        "Para calcular el crecimiento necesito que aportes algo, ya sea al inicio o en "
                        "cada periodo 🙂 Empecemos de nuevo:\n\n"
                        "1️⃣ ¿Con cuánto dinero vas a empezar a invertir? Si vas a empezar desde cero, "
                        "escribe 0. (por ejemplo: 5000)"
                    )
                contexto["inversion_aportacion"] = aportacion
                contexto["esperando"] = "inversion_tasa_anual"
                return "3️⃣ ¿Qué tasa de rendimiento ANUAL esperas obtener? (por ejemplo, si esperas un 10% anual, escribe 10)"
            except:
                return "Por favor, indica la aportación por periodo como un número (ejemplo: 500, o 0 si no vas a aportar más)."

        if contexto["esperando"] == "inversion_tasa_anual":
            try:
                tasa_anual = Decimal(mensaje.replace(",", "").replace("%", ""))
                if tasa_anual < 0:
                    return "La tasa esperada no puede ser negativa para este cálculo 🙂 Indica un número positivo (ejemplo: 10)."
                contexto["inversion_tasa_anual"] = tasa_anual
                contexto["esperando"] = "inversion_tiempo_numero"
                return "4️⃣ ¿En cuánto tiempo? Escribe solo el número (por ejemplo: 5)"
            except:
                return "Por favor, indica la tasa de rendimiento anual como un número (ejemplo: 10)."

        if contexto["esperando"] == "inversion_tiempo_numero":
            try:
                tiempo_numero = Decimal(mensaje.replace(",", ""))
                if tiempo_numero <= 0:
                    return "El tiempo debe ser mayor a cero. ¿En cuánto tiempo? (ejemplo: 5)"
                contexto["inversion_tiempo_numero"] = tiempo_numero
                contexto["esperando"] = "inversion_tiempo_unidad"
                return (
                    "¿Ese número que diste fue en meses o en años?\n"
                    "1️⃣ Meses\n"
                    "2️⃣ Años"
                )
            except:
                return "Por favor, indica el tiempo como un número (ejemplo: 5)."

        if contexto["esperando"] == "inversion_tiempo_unidad":
            if texto_limpio not in ["1", "2", "meses", "años", "anos", "año", "ano"]:
                return "Por favor, elige 1 (Meses) o 2 (Años)."
            if texto_limpio in ["1", "meses"]:
                anios = contexto["inversion_tiempo_numero"] / Decimal("12")
            else:
                anios = contexto["inversion_tiempo_numero"]
            contexto["inversion_anios"] = anios
            contexto["esperando"] = "inversion_frecuencia"
            return MENSAJE_FRECUENCIA_INVERSION

        if contexto["esperando"] == "inversion_frecuencia":
            if texto_limpio == "5":
                contexto["esperando"] = "inversion_frecuencia_otro"
                return "¿Cuántas veces al año en total aportarías? (ejemplo: 24)"
            if texto_limpio not in FRECUENCIAS_PAGO:
                return "Por favor, elige una opción del 1 al 5."
            try:
                frecuencia_label, periodos_por_anio = FRECUENCIAS_PAGO[texto_limpio]
                resultado = calcular_crecimiento_inversion(
                    contexto["inversion_monto_inicial"],
                    contexto["inversion_aportacion"],
                    contexto["inversion_anios"],
                    contexto["inversion_tasa_anual"],
                    periodos_por_anio,
                    frecuencia_label,
                )
                return _con_feedback(numero, "inversion_crecimiento", resultado)
            except Exception:
                return "Hubo un error al calcular. Revisa tus datos e intenta de nuevo."

        if contexto["esperando"] == "inversion_frecuencia_otro":
            try:
                periodos_por_anio = Decimal(mensaje.strip())
                if periodos_por_anio <= 0:
                    return "El número de veces al año debe ser mayor a cero (ejemplo: 24)."
                resultado = calcular_crecimiento_inversion(
                    contexto["inversion_monto_inicial"],
                    contexto["inversion_aportacion"],
                    contexto["inversion_anios"],
                    contexto["inversion_tasa_anual"],
                    periodos_por_anio,
                    "personalizada",
                )
                return _con_feedback(numero, "inversion_crecimiento", resultado)
            except Exception:
                return "Por favor, indica un número de veces al año (ejemplo: 24)."

        # --- Jubilación: ahorro voluntario (fórmula oficial de CONSAR/Afore) ---
        if contexto["esperando"] == "jubilacion_saldo_actual":
            try:
                contexto["jubilacion_saldo_actual"] = Decimal(mensaje.replace(",", "").replace("$", ""))
                if contexto["jubilacion_saldo_actual"] < 0:
                    return "Ese número no puede ser negativo 🙂 Si vas a empezar desde cero, escribe 0."
                contexto["esperando"] = "jubilacion_edad_actual"
                return "2️⃣ ¿Cuál es tu edad actual? (ejemplo: 30)"
            except:
                return "Por favor, escribe solo un número (ejemplo: 45000, o 0 si vas a empezar desde cero)."

        if contexto["esperando"] == "jubilacion_edad_actual":
            try:
                edad_actual = int(Decimal(mensaje.replace(",", "")))
                if edad_actual < 15 or edad_actual > 80:
                    return "Indica tu edad actual en años, entre 15 y 80 (ejemplo: 30)."
                contexto["jubilacion_edad_actual"] = edad_actual
                contexto["esperando"] = "jubilacion_edad_retiro"
                return (
                    "3️⃣ ¿A qué edad planeas retirarte?\n"
                    "1️⃣ 60 años\n"
                    "2️⃣ 65 años\n"
                    "3️⃣ 67 años"
                )
            except:
                return "Por favor, indica tu edad actual como un número (ejemplo: 30)."

        if contexto["esperando"] == "jubilacion_edad_retiro":
            mapa_edad_retiro = {"1": 60, "60": 60, "2": 65, "65": 65, "3": 67, "67": 67}
            if texto_limpio not in mapa_edad_retiro:
                return "Por favor, elige 1 (60 años), 2 (65 años) o 3 (67 años)."
            edad_retiro = mapa_edad_retiro[texto_limpio]
            if edad_retiro <= contexto["jubilacion_edad_actual"]:
                return (
                    f"Tu edad de retiro elegida ({edad_retiro} años) debe ser mayor a tu edad actual "
                    f"({contexto['jubilacion_edad_actual']} años). Elige otra opción:\n"
                    "1️⃣ 60 años\n"
                    "2️⃣ 65 años\n"
                    "3️⃣ 67 años"
                )
            contexto["jubilacion_edad_retiro"] = edad_retiro
            contexto["esperando"] = "jubilacion_genero"
            return (
                "4️⃣ Para calcular la Unidad de Renta Vitalicia (esto lo pide la metodología oficial, ya que "
                "la tabla usada distingue por esto), ¿cuál es tu sexo registrado ante tu Afore?\n"
                "1️⃣ Hombre\n"
                "2️⃣ Mujer"
            )

        if contexto["esperando"] == "jubilacion_genero":
            if texto_limpio in ["1", "hombre"]:
                contexto["jubilacion_genero"] = "hombre"
            elif texto_limpio in ["2", "mujer"]:
                contexto["jubilacion_genero"] = "mujer"
            else:
                return "Por favor, elige 1 (Hombre) o 2 (Mujer)."
            contexto["esperando"] = "jubilacion_aportacion_mensual"
            return "5️⃣ ¿Cuánto te gustaría aportar cada mes de forma voluntaria? (ejemplo: 500)"

        if contexto["esperando"] == "jubilacion_aportacion_mensual":
            try:
                contexto["jubilacion_aportacion_mensual"] = Decimal(mensaje.replace(",", "").replace("$", ""))
                if contexto["jubilacion_aportacion_mensual"] < 0:
                    return "Ese número no puede ser negativo 🙂 Si no vas a aportar nada por ahora, escribe 0."
                contexto["esperando"] = "jubilacion_rendimiento_anual"
                return (
                    "6️⃣ ¿Qué rendimiento ANUAL esperas obtener en tu cuenta, antes de comisiones? Lo puedes "
                    "ver en el estado de cuenta de tu Afore, o usar un estimado. (ejemplo: 6)"
                )
            except:
                return "Por favor, indica ese monto como un número (ejemplo: 500, o 0 si no vas a aportar nada por ahora)."

        if contexto["esperando"] == "jubilacion_rendimiento_anual":
            try:
                tasa_anual = Decimal(mensaje.replace(",", "").replace("%", ""))
                if tasa_anual < 0:
                    return "El rendimiento esperado no puede ser negativo para este cálculo 🙂 Indica un número positivo (ejemplo: 6)."
                resultado = calcular_ahorro_voluntario_afore(
                    contexto["jubilacion_saldo_actual"],
                    contexto["jubilacion_edad_actual"],
                    contexto["jubilacion_edad_retiro"],
                    contexto["jubilacion_genero"],
                    contexto["jubilacion_aportacion_mensual"],
                    tasa_anual,
                )
                return _con_feedback(numero, "jubilacion_voluntario", resultado)
            except:
                return "Por favor, indica el rendimiento anual esperado como un número (ejemplo: 6)."

        # --- Jubilación: calculadora IMSS (Régimen de Ley 97, versión simplificada) ---
        if contexto["esperando"] == "imss_saldo_actual":
            try:
                contexto["imss_saldo_actual"] = Decimal(mensaje.replace(",", "").replace("$", ""))
                if contexto["imss_saldo_actual"] < 0:
                    return "Ese número no puede ser negativo 🙂 Si vas a empezar desde cero, escribe 0."
                contexto["esperando"] = "imss_edad_actual"
                return "2️⃣ ¿Cuál es tu edad actual? (ejemplo: 30)"
            except:
                return "Por favor, escribe solo un número (ejemplo: 45000, o 0 si vas a empezar desde cero)."

        if contexto["esperando"] == "imss_edad_actual":
            try:
                edad_actual = int(Decimal(mensaje.replace(",", "")))
                if edad_actual < 15 or edad_actual > 80:
                    return "Indica tu edad actual en años, entre 15 y 80 (ejemplo: 30)."
                contexto["imss_edad_actual"] = edad_actual
                contexto["esperando"] = "imss_edad_retiro"
                return (
                    "3️⃣ ¿A qué edad planeas retirarte?\n"
                    "1️⃣ 60 años\n"
                    "2️⃣ 65 años\n"
                    "3️⃣ 67 años"
                )
            except:
                return "Por favor, indica tu edad actual como un número (ejemplo: 30)."

        if contexto["esperando"] == "imss_edad_retiro":
            mapa_edad_retiro = {"1": 60, "60": 60, "2": 65, "65": 65, "3": 67, "67": 67}
            if texto_limpio not in mapa_edad_retiro:
                return "Por favor, elige 1 (60 años), 2 (65 años) o 3 (67 años)."
            edad_retiro = mapa_edad_retiro[texto_limpio]
            if edad_retiro <= contexto["imss_edad_actual"]:
                return (
                    f"Tu edad de retiro elegida ({edad_retiro} años) debe ser mayor a tu edad actual "
                    f"({contexto['imss_edad_actual']} años). Elige otra opción:\n"
                    "1️⃣ 60 años\n"
                    "2️⃣ 65 años\n"
                    "3️⃣ 67 años"
                )
            contexto["imss_edad_retiro"] = edad_retiro
            contexto["esperando"] = "imss_genero"
            return (
                "4️⃣ Para calcular la Unidad de Renta Vitalicia (esto lo pide la metodología oficial, ya que "
                "la tabla usada distingue por esto), ¿cuál es tu sexo registrado ante tu Afore?\n"
                "1️⃣ Hombre\n"
                "2️⃣ Mujer"
            )

        if contexto["esperando"] == "imss_genero":
            if texto_limpio in ["1", "hombre"]:
                contexto["imss_genero"] = "hombre"
            elif texto_limpio in ["2", "mujer"]:
                contexto["imss_genero"] = "mujer"
            else:
                return "Por favor, elige 1 (Hombre) o 2 (Mujer)."
            contexto["esperando"] = "imss_salario_mensual"
            return (
                "5️⃣ ¿Cuál es tu salario mensual base de cotización? Es el sueldo con el que cotizas ante el "
                "IMSS (lo puedes ver en tu recibo de nómina). Este dato no se guarda ni se comparte con "
                "nadie (ni con el SAT ni con nadie más): solo se usa aquí, en este momento, para hacer el "
                "cálculo. (ejemplo: 12000)"
            )

        if contexto["esperando"] == "imss_salario_mensual":
            try:
                contexto["imss_salario_mensual"] = Decimal(mensaje.replace(",", "").replace("$", ""))
                if contexto["imss_salario_mensual"] <= 0:
                    return "Por favor, indica un salario mensual mayor a 0 (ejemplo: 12000)."
                contexto["imss_rango_salarial"] = _imss_rango_salarial_desde_salario(
                    contexto["imss_salario_mensual"]
                )
                contexto["esperando"] = "imss_rendimiento_anual"
                return (
                    "6️⃣ ¿Qué rendimiento ANUAL real esperas obtener, antes de comisiones? La metodología oficial "
                    "solo permite elegir entre estas dos opciones:\n"
                    "1️⃣ 4%\n"
                    "2️⃣ 5%"
                )
            except:
                return "Por favor, indica un número (ejemplo: 12000)."

        if contexto["esperando"] == "imss_rendimiento_anual":
            mapa_rendimiento = {"1": Decimal("4"), "4": Decimal("4"), "2": Decimal("5"), "5": Decimal("5")}
            if texto_limpio not in mapa_rendimiento:
                return "Por favor, elige 1 (4%) o 2 (5%)."
            contexto["imss_rendimiento_anual"] = mapa_rendimiento[texto_limpio]
            contexto["esperando"] = "imss_aportacion_voluntaria_mensual"
            return (
                "7️⃣ Además de tus aportaciones obligatorias, ¿te gustaría aportar algo cada mes de forma "
                "voluntaria? Si no, escribe 0. (ejemplo: 500)"
            )

        if contexto["esperando"] == "imss_aportacion_voluntaria_mensual":
            try:
                aportacion_voluntaria = Decimal(mensaje.replace(",", "").replace("$", ""))
                if aportacion_voluntaria < 0:
                    return "Ese número no puede ser negativo 🙂 Si no vas a aportar nada extra, escribe 0."
                resultado = calcular_jubilacion_imss(
                    contexto["imss_saldo_actual"],
                    contexto["imss_edad_actual"],
                    contexto["imss_edad_retiro"],
                    contexto["imss_genero"],
                    contexto["imss_salario_mensual"],
                    contexto["imss_rango_salarial"],
                    contexto["imss_rendimiento_anual"],
                    aportacion_voluntaria,
                )
                return _con_feedback(numero, "jubilacion_imss", resultado)
            except:
                return "Por favor, indica ese monto como un número (ejemplo: 500, o 0 si no vas a aportar nada extra)."

        # --- Jubilación: calculadora ISSSTE (Régimen de cuentas individuales, versión simplificada) ---
        if contexto["esperando"] == "issste_saldo_actual":
            try:
                contexto["issste_saldo_actual"] = Decimal(mensaje.replace(",", "").replace("$", ""))
                if contexto["issste_saldo_actual"] < 0:
                    return "Ese número no puede ser negativo 🙂 Si vas a empezar desde cero, escribe 0."
                contexto["esperando"] = "issste_edad_actual"
                return "2️⃣ ¿Cuál es tu edad actual? (ejemplo: 30)"
            except:
                return "Por favor, escribe solo un número (ejemplo: 45000, o 0 si vas a empezar desde cero)."

        if contexto["esperando"] == "issste_edad_actual":
            try:
                edad_actual = int(Decimal(mensaje.replace(",", "")))
                if edad_actual < 15 or edad_actual > 80:
                    return "Indica tu edad actual en años, entre 15 y 80 (ejemplo: 30)."
                contexto["issste_edad_actual"] = edad_actual
                contexto["esperando"] = "issste_edad_retiro"
                return (
                    "3️⃣ ¿A qué edad planeas retirarte? La metodología del ISSSTE solo permite elegir entre "
                    "estas opciones:\n"
                    "1️⃣ 65 años\n"
                    "2️⃣ 66 años\n"
                    "3️⃣ 67 años"
                )
            except:
                return "Por favor, indica tu edad actual como un número (ejemplo: 30)."

        if contexto["esperando"] == "issste_edad_retiro":
            mapa_edad_retiro = {"1": 65, "65": 65, "2": 66, "66": 66, "3": 67, "67": 67}
            if texto_limpio not in mapa_edad_retiro:
                return "Por favor, elige 1 (65 años), 2 (66 años) o 3 (67 años)."
            edad_retiro = mapa_edad_retiro[texto_limpio]
            if edad_retiro <= contexto["issste_edad_actual"]:
                return (
                    f"Tu edad de retiro elegida ({edad_retiro} años) debe ser mayor a tu edad actual "
                    f"({contexto['issste_edad_actual']} años). Elige otra opción:\n"
                    "1️⃣ 65 años\n"
                    "2️⃣ 66 años\n"
                    "3️⃣ 67 años"
                )
            contexto["issste_edad_retiro"] = edad_retiro
            contexto["esperando"] = "issste_genero"
            return (
                "4️⃣ Para calcular la Unidad de Renta Vitalicia (esto lo pide la metodología oficial, ya que "
                "la tabla usada distingue por esto), ¿cuál es tu sexo registrado ante tu Afore?\n"
                "1️⃣ Hombre\n"
                "2️⃣ Mujer"
            )

        if contexto["esperando"] == "issste_genero":
            if texto_limpio in ["1", "hombre"]:
                contexto["issste_genero"] = "hombre"
            elif texto_limpio in ["2", "mujer"]:
                contexto["issste_genero"] = "mujer"
            else:
                return "Por favor, elige 1 (Hombre) o 2 (Mujer)."
            contexto["esperando"] = "issste_sueldo_basico_mensual"
            return (
                "5️⃣ ¿Cuál es tu sueldo básico mensual? Lo puedes ver en tu recibo de nómina o en tu estado "
                "de cuenta de PENSIONISSSTE. Este dato no se guarda ni se comparte con nadie (ni con el SAT "
                "ni con nadie más): solo se usa aquí, en este momento, para hacer el cálculo. "
                "(ejemplo: 15000)"
            )

        if contexto["esperando"] == "issste_sueldo_basico_mensual":
            try:
                contexto["issste_sueldo_basico_mensual"] = Decimal(mensaje.replace(",", "").replace("$", ""))
                if contexto["issste_sueldo_basico_mensual"] <= 0:
                    return "Por favor, indica un sueldo mensual mayor a 0 (ejemplo: 15000)."
                contexto["esperando"] = "issste_ahorro_solidario"
                return (
                    "6️⃣ ¿Quieres aportar a tu ahorro solidario? Por cada peso que tú aportes, el Gobierno "
                    "Federal aporta 3.25 pesos más (hasta cierto tope), así que suele convenir aportar el "
                    "máximo si puedes:\n"
                    "1️⃣ No voy a aportar (0%)\n"
                    "2️⃣ 1% de mi sueldo\n"
                    "3️⃣ 2% de mi sueldo"
                )
            except:
                return "Por favor, indica un número (ejemplo: 15000)."

        if contexto["esperando"] == "issste_ahorro_solidario":
            mapa_ahorro_solidario = {
                "1": Decimal("0"), "0": Decimal("0"),
                "2": Decimal("1"), "1%": Decimal("1"),
                "3": Decimal("2"), "2%": Decimal("2"),
            }
            if texto_limpio not in mapa_ahorro_solidario:
                return "Por favor, elige 1 (no voy a aportar), 2 (1%) o 3 (2%)."
            contexto["issste_ahorro_solidario"] = mapa_ahorro_solidario[texto_limpio]
            contexto["esperando"] = "issste_bono_pension"
            return (
                "7️⃣ ¿Tienes un Bono de Pensión ISSSTE? Es un monto que se te reconoce si ya cotizabas antes "
                "de la reforma de 2007; aparece en tu estado de cuenta de PENSIONISSSTE. Si no tienes o no "
                "sabes, escribe 0. (ejemplo: 50000)"
            )

        if contexto["esperando"] == "issste_bono_pension":
            try:
                contexto["issste_bono_pension"] = Decimal(mensaje.replace(",", "").replace("$", ""))
                if contexto["issste_bono_pension"] < 0:
                    return "Ese número no puede ser negativo 🙂 Si no tienes Bono de Pensión ISSSTE, escribe 0."
                contexto["esperando"] = "issste_rendimiento_anual"
                return (
                    "8️⃣ ¿Qué rendimiento ANUAL real esperas obtener, antes de comisiones? La metodología "
                    "oficial solo permite elegir entre estas dos opciones:\n"
                    "1️⃣ 4%\n"
                    "2️⃣ 5%"
                )
            except:
                return "Por favor, indica ese monto como un número (ejemplo: 50000, o 0 si no tienes)."

        if contexto["esperando"] == "issste_rendimiento_anual":
            mapa_rendimiento = {"1": Decimal("4"), "4": Decimal("4"), "2": Decimal("5"), "5": Decimal("5")}
            if texto_limpio not in mapa_rendimiento:
                return "Por favor, elige 1 (4%) o 2 (5%)."
            resultado = calcular_jubilacion_issste(
                contexto["issste_saldo_actual"],
                contexto["issste_edad_actual"],
                contexto["issste_edad_retiro"],
                contexto["issste_genero"],
                contexto["issste_sueldo_basico_mensual"],
                contexto["issste_ahorro_solidario"],
                contexto["issste_bono_pension"],
                mapa_rendimiento[texto_limpio],
            )
            return _con_feedback(numero, "jubilacion_issste", resultado)

        # --- Jubilación: calculadora Ley 73 (IMSS, régimen anterior a 1997) ---
        if contexto["esperando"] == "ley73_salario_promedio":
            try:
                contexto["ley73_salario_promedio"] = Decimal(mensaje.replace(",", "").replace("$", ""))
                if contexto["ley73_salario_promedio"] <= 0:
                    return "Por favor, indica un salario mensual mayor a 0 (ejemplo: 15000)."
                contexto["esperando"] = "ley73_semanas_cotizadas"
                return (
                    "2️⃣ ¿Cuántas semanas has cotizado en total al IMSS? Se necesitan al menos 500 para "
                    "tener derecho a esta pensión. Lo puedes consultar en la app del IMSS Digital. "
                    "(ejemplo: 1200)"
                )
            except:
                return "Por favor, indica un número (ejemplo: 15000)."

        if contexto["esperando"] == "ley73_semanas_cotizadas":
            try:
                semanas = Decimal(mensaje.replace(",", ""))
                if semanas < 0:
                    return "Ese número no puede ser negativo 🙂 Indica tus semanas cotizadas (ejemplo: 1200)."
                if semanas < 500:
                    return (
                        "Para este tipo de pensión se necesitan al menos 500 semanas cotizadas, y tú "
                        f"indicaste {semanas}. Si crees que es un error, revisa tus semanas exactas en la "
                        "app del IMSS Digital y vuelve a intentarlo, o escribe *menú* para salir."
                    )
                contexto["ley73_semanas_cotizadas"] = semanas
                contexto["esperando"] = "ley73_edad_retiro"
                return (
                    "3️⃣ ¿A qué edad planeas retirarte? La Ley 73 solo permite pensionarte entre los 60 y "
                    "los 65 años. Escribe un número de 60 a 65."
                )
            except:
                return "Por favor, indica un número (ejemplo: 1200)."

        if contexto["esperando"] == "ley73_edad_retiro":
            try:
                edad_retiro = int(Decimal(mensaje.replace(",", "")))
                if edad_retiro not in LEY73_PORCENTAJE_PENSION_POR_EDAD:
                    return "Por favor, indica una edad de retiro entre 60 y 65 años (ejemplo: 65)."
                resultado = calcular_jubilacion_ley73(
                    contexto["ley73_salario_promedio"],
                    contexto["ley73_semanas_cotizadas"],
                    edad_retiro,
                )
                return _con_feedback(numero, "jubilacion_ley73", resultado)
            except:
                return "Por favor, indica una edad de retiro entre 60 y 65 años (ejemplo: 65)."


        # FLUJO 2: abonos extra directos
        if contexto["esperando"] == "monto2":
            try:
                contexto["monto"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa_anual2"
                return (
                    "¿Cuál es la tasa de interés ANUAL que te ofrecieron?\n"
                    "Es la que normalmente te dicen en el banco o la tienda (ejemplo: si te "
                    "dijeron 45% anual, solo escribe 45)."
                )
            except:
                return "Por favor, indica el monto del crédito como un número."

        if contexto["esperando"] == "tasa_anual2":
            try:
                contexto["tasa_anual"] = Decimal(mensaje.replace(",", "").replace("%", ""))
                contexto["esperando"] = "anios2"
                return "¿A cuántos años es el crédito? (puedes usar decimales, ejemplo: 2.5)"
            except:
                return "Por favor, indica la tasa anual como un número (ejemplo: 45)."

        if contexto["esperando"] == "anios2":
            try:
                contexto["anios"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "frecuencia2"
                return MENSAJE_FRECUENCIA
            except:
                return "Por favor, indica los años como un número (ejemplo: 2.5)."

        if contexto["esperando"] == "frecuencia2":
            if texto_limpio == "5":
                contexto["esperando"] = "frecuencia_otro2"
                return "¿Cuántos pagos haces al año en total? (ejemplo: 24)"
            if texto_limpio not in FRECUENCIAS_PAGO:
                return "Por favor, elige una opción del 1 al 5."
            try:
                frecuencia_label, periodos_por_anio = FRECUENCIAS_PAGO[texto_limpio]
                return _resolver_frecuencia_flujo2(contexto, frecuencia_label, periodos_por_anio)
            except Exception:
                return "Hubo un error al calcular. Revisa tus datos e intenta de nuevo."

        if contexto["esperando"] == "frecuencia_otro2":
            try:
                periodos_por_anio = Decimal(mensaje.strip())
                return _resolver_frecuencia_flujo2(contexto, "personalizada", periodos_por_anio)
            except Exception:
                return "Por favor, indica un número de pagos al año (ejemplo: 24)."

        if contexto["esperando"] == "abono_extra2":
            try:
                contexto["abono"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "desde2"
                return "¿A partir de qué periodo comenzarás a abonar esa cantidad extra? (Ejemplo: 4)"
            except:
                return "Por favor, escribe solo la cantidad del abono extra (ejemplo: 500)"

        if contexto["esperando"] == "desde2":
            try:
                desde = int(mensaje.strip())
                total_sin, total_con, ahorro, pagos_menos = calcular_ahorro_por_abonos(
                    contexto["monto"], contexto["tasa"],
                    contexto["plazo"], contexto["abono"], desde
                )
                resultado = (
                    f"💸 Si pagaras este crédito sin hacer abonos extra, terminarías pagando ${float(total_sin):,.2f} en total.\n"
                    f"Pero si decides abonar ${float(contexto['abono']):,.2f} adicionales por periodo desde el periodo {desde}...\n"
                    f"✅ Terminarías de pagar en menos tiempo (¡te ahorras {pagos_menos} pagos!)\n"
                    f"💰 Pagarías ${float(total_con):,.2f} en total\n"
                    f"🧮 Y te ahorrarías ${float(ahorro):,.2f} solo en intereses.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
                return _con_feedback(numero, "credito_pagos_extra", resultado)
            except:
                return "Uy, algo no cuadró con esos datos 🤔 Revisa que hayas escrito solo números y vuelve a intentarlo, o escribe *menú* para empezar de nuevo."

        # FLUJO 1: Simular crédito
        if contexto["esperando"] == "monto_credito":
            try:
                contexto["monto"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa_anual_credito"
                return (
                    "¿Cuál es la tasa de interés ANUAL que te ofrecieron?\n"
                    "Es la que normalmente te dicen en el banco o la tienda (ejemplo: si te "
                    "dijeron 45% anual, solo escribe 45)."
                )
            except:
                return "Por favor, indica el monto como un número (ejemplo: 100000)"

        if contexto["esperando"] == "tasa_anual_credito":
            try:
                contexto["tasa_anual"] = Decimal(mensaje.replace(",", "").replace("%", ""))
                contexto["esperando"] = "anios_credito"
                return "¿A cuántos años es el crédito? (puedes usar decimales, ejemplo: 2.5)"
            except:
                return "Por favor, indica la tasa anual como un número (ejemplo: 45)."

        if contexto["esperando"] == "anios_credito":
            try:
                contexto["anios"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "frecuencia_credito"
                return MENSAJE_FRECUENCIA
            except:
                return "Por favor, indica los años como un número (ejemplo: 2.5)."

        if contexto["esperando"] == "frecuencia_credito":
            if texto_limpio == "5":
                contexto["esperando"] = "frecuencia_otro_credito"
                return "¿Cuántos pagos haces al año en total? (ejemplo: 24)"
            if texto_limpio not in FRECUENCIAS_PAGO:
                return "Por favor, elige una opción del 1 al 5."
            try:
                frecuencia_label, periodos_por_anio = FRECUENCIAS_PAGO[texto_limpio]
                return _resolver_frecuencia_flujo1(contexto, frecuencia_label, periodos_por_anio)
            except Exception:
                return "Hubo un error al calcular. Revisa tus datos e intenta de nuevo."

        if contexto["esperando"] == "frecuencia_otro_credito":
            try:
                periodos_por_anio = Decimal(mensaje.strip())
                return _resolver_frecuencia_flujo1(contexto, "personalizada", periodos_por_anio)
            except Exception:
                return "Por favor, indica un número de pagos al año (ejemplo: 24)."

        if contexto["esperando"] == "ver_si_abonos1":
            if texto_limpio in ["si", "sí"]:
                contexto["esperando"] = "abono_extra1"
                return "¿Cuánto deseas abonar extra por periodo? (Ejemplo: 500)"
            elif texto_limpio == "no":
                estado_usuario.pop(numero)
                return "Ok, regresamos al inicio. Escribe *menú* si deseas ver otras opciones."
            else:
                return "Por favor, responde *sí* o *no*."

        if contexto["esperando"] == "abono_extra1":
            try:
                contexto["abono"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "desde_cuando1"
                return "¿A partir de qué periodo comenzarás a abonar esa cantidad extra? (Ejemplo: 4)"
            except:
                return "Por favor, un número válido (ej: 500)"

        if contexto["esperando"] == "desde_cuando1":
            try:
                desde = int(mensaje.strip())
                total_sin, total_con, ahorro, pagos_menos = calcular_ahorro_por_abonos(
                    contexto["monto"], contexto["tasa"],
                    contexto["plazo"], contexto["abono"], desde
                )
                resultado = (
                    f"💸 Si pagaras este crédito sin hacer abonos extra, terminarías pagando ${float(total_sin):,.2f} en total.\n\n"
                    f"Pero si decides abonar ${float(contexto['abono']):,.2f} adicionales por periodo desde el periodo {desde}...\n"
                    f"✅ Terminarías de pagar en menos tiempo (¡te ahorras {pagos_menos} pagos!)\n"
                    f"💰 Pagarías ${float(total_con):,.2f} en total\n"
                    f"🧮 Y te ahorrarías ${float(ahorro):,.2f} solo en intereses.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
                return _con_feedback(numero, "credito_pagos_extra", resultado)
            except:
                return "Uy, algo no cuadró con esos datos 🤔 Revisa que hayas escrito solo números y vuelve a intentarlo, o escribe *menú* para empezar de nuevo."

                # Opción 3 (compras a pagos fijos)
        if contexto["esperando"] == "precio_contado":
            try:
                contexto["precio_contado"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "pago_fijo_tienda"
                return "2️⃣ ¿De cuánto será cada pago (por ejemplo: 250)?"
            except:
                return "Por favor, indica el precio de contado con números (ejemplo: 1800)"

        if contexto["esperando"] == "pago_fijo_tienda":
            try:
                contexto["pago_fijo_tienda"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "numero_pagos_tienda"
                return "3️⃣ ¿Cuántos pagos harás en total? (ejemplo: 24)"
            except:
                return "Por favor, escribe solo el número del pago (ejemplo: 250)."

        # PRIMER PASO: guardamos num_pagos y pedimos periodos anuales
        if contexto["esperando"] == "numero_pagos_tienda":
            try:
                numero_pagos = int(mensaje.strip())
                contexto["numero_pagos_tienda"] = numero_pagos

                # Preguntamos cuántos periodos hay en 1 año, para calcular la tasa anual real
                contexto["esperando"] = "pedir_periodos_anuales_tienda"
                return (
                    "Para calcular la tasa anual real, necesito saber cuántos periodos hay en 1 año.\n"
                    "Por ejemplo:\n"
                    "• 12 si es mensual\n"
                    "• 24 si es quincenal (cada 15 días)\n"
                    "• 26 si es catorcenal (cada 14 días)\n"
                    "• 52 si es semanal\n\n"
                    "Escribe solo el número:"
                )
            except:
                return "Ocurrió un error. Indica cuántos pagos totales harás (ejemplo: 24)."

        # SEGUNDO PASO: usuario indica periodos anuales
        if contexto["esperando"] == "pedir_periodos_anuales_tienda":
            try:
                periodos_anuales = int(mensaje.strip())
                contexto["periodos_anuales"] = periodos_anuales

                mensaje_resultado = calcular_costo_credito_tienda(
                    contexto["precio_contado"],
                    contexto["pago_fijo_tienda"],
                    contexto["numero_pagos_tienda"],
                    periodos_anuales
                )

                return _con_feedback(numero, "credito_costo_meses", mensaje_resultado)

            except Exception as e:
                print(f"Error al calcular tasa anual: {e}")
                return "Ocurrió un error. Asegúrate de indicar cuántos periodos hay en un año con un número (ej: 24)."


        # Opción 4 (capacidad de pago)
        if contexto["esperando"] == "ingreso":
            try:
                contexto["ingreso"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "pagos_fijos"
                return (
                    "2️⃣ ¿Cuánto pagas mensualmente en créditos formales o instituciones financieras?\n"
                    "(Es decir, en pagos de préstamos personales, hipotecas, crédito de auto, crédito de "
                    "nómina, etc.) Si no tienes ninguno, escribe 0. (ejemplo: 1800)"
                )
            except:
                return "Por favor, escribe un número válido (ej: 12500)"

        if contexto["esperando"] == "pagos_fijos":
            try:
                contexto["pagos_fijos"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "deuda_revolvente"
                return (
                    "3️⃣ ¿Cuánto debes actualmente en tarjetas de crédito u otras deudas revolventes?\n"
                    "(Las deudas revolventes son las que no tienen una fecha fija para terminarse de "
                    "pagar, como las tarjetas de crédito: vas pagando lo que usas cada mes.)\n"
                    "Si no tienes ninguna, escribe 0. (ejemplo: 5000)"
                )
            except:
                return "Por favor, indica la cantidad mensual que pagas en créditos (ej: 1800)"

        if contexto["esperando"] == "deuda_revolvente":
            try:
                contexto["deuda_revolvente"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "riesgo"
                return (
                    "4️⃣ Por último, sé honesto/a contigo mismo/a: ¿cómo describirías tu forma de pagar "
                    "tus deudas hasta ahora?\n"
                    "1. Puntual (casi siempre pago a tiempo)\n"
                    "2. A veces me atraso (pero no es lo común)\n"
                    "3. Se me complica seguido (me atraso con frecuencia o ya tengo varias deudas)\n\n"
                    "No hay respuesta incorrecta, esto solo nos ayuda a calcular un número realista contigo."
                )
            except:
                return (
                    "Por favor, escribe solo el número de esa deuda (ejemplo: 5000). "
                    "Si no tienes deudas de este tipo, escribe 0."
                )

        if contexto["esperando"] == "riesgo":
            if texto_limpio not in ["1", "2", "3"]:
                return "Por favor, elige la opción 1, 2 o 3 según cómo describirías tu forma de pagar."

            contexto["riesgo"] = texto_limpio
            porcentajes = {"1": Decimal("0.60"), "2": Decimal("0.45"), "3": Decimal("0.30")}
            porcentaje_riesgo = porcentajes[texto_limpio]
            ingreso = contexto["ingreso"]
            pagos_fijos = contexto["pagos_fijos"]
            deuda_revolvente = contexto["deuda_revolvente"]
            pago_est_deuda_revolvente = deuda_revolvente * Decimal("0.06")

            capacidad_total = ingreso * porcentaje_riesgo
            capacidad_mensual = capacidad_total - pagos_fijos - pago_est_deuda_revolvente
            capacidad_mensual = capacidad_mensual.quantize(Decimal("0.01"))

            if capacidad_mensual <= 0:
                faltante = -capacidad_mensual
                resultado = (
                    f"📊 Con tus datos actuales, tus pagos fijos y el pago mínimo estimado de tus deudas "
                    f"revolventes ya superan por ${faltante:,.2f} al mes lo que se considera manejable de "
                    "tu ingreso. Esto no solo significa que por ahora no te recomendaría tomar un crédito "
                    "nuevo, sino que es muy probable que tampoco te lo aprueben, porque tu capacidad de pago "
                    "disponible ya está en números negativos.\n\n"
                    "💡 Antes de solicitar un crédito nuevo, podría convenirte enfocarte primero en bajar "
                    "tus deudas actuales. Dentro de *Crédito* tengo consejos para pagar sin ahogarte que "
                    "te pueden servir.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
                return _con_feedback(numero, "credito_cuanto_prestan", resultado)

            contexto["capacidad_mensual"] = capacidad_mensual
            contexto["porcentaje_riesgo"] = porcentaje_riesgo
            contexto["esperando"] = "subopcion_prestamo"

            return (
                f"✅ Según tus datos, podrías pagar hasta ${capacidad_mensual:,.2f} al mes en un nuevo crédito.\n\n"
                "¿Qué te gustaría hacer ahora?\n"
                "1. Calcular el monto máximo de crédito que podrías solicitar\n"
                "2. Validar si un crédito que te interesa podría ser aprobado\n"
                "Escribe 1 o 2 para continuar."
            )

        if contexto["esperando"] == "subopcion_prestamo":
            if texto_limpio == "1":
                contexto["esperando"] = "tasa_anual_simular"
                return (
                    "📈 ¿Qué tasa de interés ANUAL manejan los créditos que te interesan?\n"
                    "(ejemplo: si es 45% anual, escribe 45)"
                )
            elif texto_limpio == "2":
                contexto["esperando"] = "monto_credito_deseado"
                return "💰 ¿De cuánto sería el crédito que te interesa solicitar? (ejemplo: 150000)"
            else:
                return "Por favor, escribe 1 o 2."

        if contexto["esperando"] == "tasa_anual_simular":
            try:
                contexto["tasa_anual_simular"] = Decimal(mensaje.replace(",", "").replace("%", ""))
                contexto["esperando"] = "anios_simular"
                return "📆 ¿A cuántos años quieres simular el crédito? (ejemplo: 3)"
            except:
                return "Por favor, indica la tasa anual como un número (ejemplo: 45)."

        if contexto["esperando"] == "anios_simular":
            try:
                contexto["anios_simular"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "frecuencia_simular"
                return MENSAJE_FRECUENCIA
            except:
                return "Por favor, indica los años como un número (ejemplo: 3)."

        # submenú para el monto máximo
        if contexto["esperando"] == "frecuencia_simular":
            if texto_limpio == "5":
                contexto["esperando"] = "frecuencia_otro_simular"
                return "¿Cuántos pagos haces al año en total? (ejemplo: 24)"
            if texto_limpio not in FRECUENCIAS_PAGO:
                return "Por favor, elige una opción del 1 al 5."
            try:
                frecuencia_label, periodos_por_anio = FRECUENCIAS_PAGO[texto_limpio]
                return _resolver_frecuencia_monto_maximo(contexto, frecuencia_label, periodos_por_anio)
            except Exception:
                return "Hubo un error al calcular. Revisa tus datos e intenta de nuevo."

        if contexto["esperando"] == "frecuencia_otro_simular":
            try:
                periodos_por_anio = Decimal(mensaje.strip())
                return _resolver_frecuencia_monto_maximo(contexto, "personalizada", periodos_por_anio)
            except Exception:
                return "Por favor, indica un número de pagos al año (ejemplo: 24)."

        if contexto["esperando"] == "submenu_despues_de_maximo":
            if texto_limpio == "1":
                contexto["esperando"] = "monto_credito_deseado"
                return "💰 ¿De cuánto sería el crédito que te interesa solicitar? (ejemplo: 150000)"
            elif texto_limpio == "2":
                estado_usuario.pop(numero)
                return "Listo, escribe *menú* para ver más opciones."
            else:
                return "Por favor, escribe 1 o 2."

        if contexto["esperando"] == "monto_credito_deseado":
            try:
                contexto["monto_deseado"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa_anual_deseada"
                return (
                    "📈 ¿Cuál es la tasa de interés ANUAL de ese crédito?\n"
                    "(ejemplo: si te dijeron 45% anual, escribe 45)"
                )
            except:
                return "Por favor, indica el monto como un número (ejemplo: 150000)."

        if contexto["esperando"] == "tasa_anual_deseada":
            try:
                contexto["tasa_anual_deseada"] = Decimal(mensaje.replace(",", "").replace("%", ""))
                contexto["esperando"] = "anios_deseado"
                return "📆 ¿En cuántos años planeas pagarlo?"
            except:
                return "Por favor, indica la tasa anual como un número (ejemplo: 45)."

        if contexto["esperando"] == "anios_deseado":
            try:
                contexto["anios_deseado"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "frecuencia_deseada"
                return MENSAJE_FRECUENCIA
            except:
                return "Por favor, indica los años como un número (ejemplo: 3)."

        if contexto["esperando"] == "frecuencia_deseada":
            if texto_limpio == "5":
                contexto["esperando"] = "frecuencia_otro_deseada"
                return "¿Cuántos pagos haces al año en total? (ejemplo: 24)"
            if texto_limpio not in FRECUENCIAS_PAGO:
                return "Por favor, elige una opción del 1 al 5."
            try:
                frecuencia_label, periodos_por_anio = FRECUENCIAS_PAGO[texto_limpio]
                resultado = _resolver_frecuencia_deseado(contexto, frecuencia_label, periodos_por_anio)
                return _con_feedback(numero, "credito_cuanto_prestan", resultado)
            except Exception:
                return "Uy, algo no cuadró con esos datos 🤔 Revisa que hayas escrito solo números y vuelve a intentarlo, o escribe *menú* para empezar de nuevo."

        if contexto["esperando"] == "frecuencia_otro_deseada":
            try:
                periodos_por_anio = Decimal(mensaje.strip())
            except Exception:
                return "Por favor, indica un número de pagos al año (ejemplo: 24)."
            try:
                resultado = _resolver_frecuencia_deseado(contexto, "personalizada", periodos_por_anio)
                return _con_feedback(numero, "credito_cuanto_prestan", resultado)
            except Exception:
                return "Uy, algo no cuadró con esos datos 🤔 Revisa que hayas escrito solo números y vuelve a intentarlo, o escribe *menú* para empezar de nuevo."

        # Submenú Buró
        if contexto["esperando"] == "submenu_buro":
            if texto_limpio in ["si", "sí"]:
                estado_usuario.pop(numero)
                return (
                    "¿Cómo mejorar mi historial crediticio?\n"
                    "Aquí tienes algunos consejos prácticos para mejorar tu score en Buró de Crédito y tener un historial más saludable 📈\n"
                    "________________________________________\n"
                    "🔹 1. Paga a tiempo, siempre\n"
                    "📌 Aunque sea el pago mínimo, evita atrasarte.\n"
                    "✅ La puntualidad pesa mucho en tu historial.\n"
                    "________________________________________\n"
                    "🔹 2. Usa tus tarjetas con moderación\n"
                    "📌 Trata de no usar más del 30%-40% del límite de tu tarjeta.\n"
                    "✅ Usarlas hasta el tope te resta puntos, aunque pagues.\n"
                    "________________________________________\n"
                    "🔹 3. No abras muchos créditos al mismo tiempo\n"
                    "📌 Si pides varios préstamos en poco tiempo, parecerá que estás desesperado/a por dinero.\n"
                    "✅ Ve uno a la vez y maneja bien el que tienes.\n"
                    "________________________________________\n"
                    "🔹 4. Usa algún crédito, aunque sea pequeño\n"
                    "📌 Si no tienes historial, nunca tendrás score.\n"
                    "✅ Una tarjeta departamental o un plan telefónico pueden ser un buen inicio si los manejas bien.\n"
                    "________________________________________\n"
                    "🔹 5. Revisa tu historial al menos una vez al año\n"
                    "📌 Puedes pedir un reporte gratuito en www.burodecredito.com.mx\n"
                    "✅ Asegúrate de que no haya errores y de que tus datos estén correctos.\n"
                    "Escribe *menú*."
                )
            else:
                estado_usuario.pop(numero)
                return "Entiendo. Escribe *menú*."

    # Sin conversación activa (primera vez, ya terminó una consulta, o se
    # perdió por un reinicio del servicio): damos la bienvenida sin depender
    # de que adivine la palabra "hola", salvo que su mensaje parezca la
    # respuesta a una pregunta de un flujo que ya no recordamos.
    if numero not in estado_usuario:
        estado_usuario[numero] = {}
        if _parece_respuesta_de_conversacion_perdida(texto_limpio):
            return mensaje_sesion_reiniciada
        return saludo_inicial

    # Si sí hay una conversación activa pero no reconocimos la respuesta:
    return (
        "No entendí ese mensaje 🙏 Escribe *menú* para ver todas las opciones, o revisa que tu "
        "respuesta sea del tipo que te pedí (por ejemplo, solo números si te pedí una cantidad)."
    )

# =========================================
# "Explícamelo más fácil": simplifica los términos técnicos de la última
# respuesta del bot.
# =========================================
def _sin_acentos(texto):
    return ''.join(
        c for c in unicodedata.normalize('NFD', texto)
        if unicodedata.category(c) != 'Mn'
    )

_FRASES_EXPLICAR_MAS_FACIL = [
    "explicamelo mas facil", "explicame mas facil", "explicamelo mas sencillo",
    "explicame mas sencillo", "explicamelo de otra forma", "no entendi",
    "mas facil", "mas sencillo",
]

def es_peticion_explicar_mas_facil(texto_limpio):
    texto_sin_acentos = _sin_acentos(texto_limpio)
    return any(frase in texto_sin_acentos for frase in _FRASES_EXPLICAR_MAS_FACIL)

def _explicar_mas_facil(numero):
    ultimo = _ultimo_mensaje_bot.get(numero)
    if ultimo is None:
        # Primera vez que escribe: no hay nada que explicarle más fácil todavía.
        estado_usuario[numero] = {}
        return saludo_inicial
    terminos = buscar_terminos_glosario(ultimo)
    if not terminos:
        return (
            "Con gusto 🙂 Pero no encontré ningún término técnico en lo último que te escribí. Si hay "
            "algo puntual que no te quedó claro, cuéntame qué palabra o parte no entendiste, o escribe "
            "*glosario* para ver los términos financieros más comunes explicados de forma simple."
        )
    explicacion = "\n\n".join(f"🔑 *{nombre}*\n{exp}" for nombre, exp in terminos)
    return (
        "🧠 Con gusto, aquí te explico más sencillo algunos términos que mencioné:\n\n"
        f"{explicacion}\n\n"
        "Puedes responder tu pregunta normal cuando quieras continuar, o escribir *menú* para regresar al inicio."
    )

def procesar_mensaje(mensaje, numero):
    """
    Punto de entrada público: intercepta las peticiones de "explícamelo más
    fácil" (sin importar en qué parte de la conversación esté la persona, y
    sin modificar su estado, para no interrumpir un flujo en curso) y, si no
    aplica, delega en la lógica normal de la conversación. Además guarda la
    respuesta del bot como "el último mensaje" para poder simplificarla si
    la piden después, y registra (de forma anónima) el cambio de paso para
    la analítica de uso.
    """
    # Si esta persona no está en memoria, puede ser realmente nueva, o puede
    # ser que el servicio se haya reiniciado a la mitad de su conversación:
    # antes de asumir lo primero, intentamos recuperar su estado guardado.
    if numero not in estado_usuario:
        estado_recuperado = _cargar_estado_sesion(numero)
        if estado_recuperado is not None:
            estado_usuario[numero] = estado_recuperado

    texto_limpio = _MODIFICADORES_EMOJI_RE.sub('', _BORDE_PUNTUACION_RE.sub('', mensaje).lower())
    if es_peticion_explicar_mas_facil(texto_limpio):
        estado_actual = estado_usuario.get(numero, {}).get("esperando")
        _registrar_evento_uso(numero, estado_actual, "explicamelo_mas_facil")
        return _explicar_mas_facil(numero)

    # "(primer_contacto)" en vez de None: así el primer mensaje de alguien
    # nuevo también queda registrado como un cambio de paso real (si no,
    # pasar de "no existe en estado_usuario" a "esperando: None" se vería
    # igual que "None a None", o sea que no se registraría nada).
    es_primer_contacto = numero not in estado_usuario
    estado_antes = "(primer_contacto)" if es_primer_contacto else estado_usuario.get(numero, {}).get("esperando")

    respuesta = _procesar_mensaje_interno(mensaje, numero)
    _ultimo_mensaje_bot[numero] = respuesta
    _guardar_estado_sesion(numero)

    estado_despues = estado_usuario.get(numero, {}).get("esperando")
    if es_primer_contacto or estado_antes != estado_despues:
        _registrar_evento_uso(numero, estado_antes, estado_despues)

    return respuesta

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if verify_token == VERIFY_TOKEN:
            return str(challenge), 200
        return "Token inválido", 403

    if request.method == "POST":
        data = request.get_json()
        print("📩 Webhook recibido:")
        print(json.dumps(data, indent=2))

        try:
            mensaje = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
            numero = data['entry'][0]['changes'][0]['value']['messages'][0]['from']
            message_id = data['entry'][0]['changes'][0]['value']['messages'][0].get('id')
        except Exception as e:
            print("⚠️ No se pudo procesar el mensaje:", e)
            return "ok", 200

        # Evita responder dos veces al mismo mensaje reenviado (ver protección
        # contra mensajes duplicados, arriba).
        if ya_fue_procesado(message_id):
            print(f"⚠️ Mensaje duplicado ignorado (id={message_id})")
            return {"status": "duplicado_ignorado"}, 200

        respuesta = procesar_mensaje(mensaje, numero)
        enviar_mensaje(numero, respuesta)

        return {
            "status": "success",
            "respuesta_bot": respuesta
        }, 200
