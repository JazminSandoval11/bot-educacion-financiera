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
from collections import deque
from decimal import Decimal, getcontext, ROUND_HALF_UP
from math import log
import requests  # <-- AÑADIDO

# Quita signos de puntuación y espacios sueltos al inicio/final de un mensaje
# (¡Hola!, Hola., ¿menú? etc. deben reconocerse igual que "hola").
_BORDE_PUNTUACION_RE = re.compile(r'^[\s¡!¿?.,;:()"\']+|[\s¡!¿?.,;:()"\']+$')

app = Flask(__name__)
getcontext().prec = 17  # Precisión tipo Excel

# Token, ID de número y verify token se leen de variables de entorno
# (configúralas en Render → tu servicio → Environment).
# NUNCA escribas valores reales aquí directamente.
TOKEN = os.environ.get('WHATSAPP_TOKEN')
PHONE_NUMBER_ID = os.environ.get('WHATSAPP_PHONE_NUMBER_ID')
VERIFY_TOKEN = os.environ.get('WHATSAPP_VERIFY_TOKEN', 'arrocito2024')

# Ruta para validar que el sitio está activo (solución para Meta y og:image)
@app.route('/')
def index():
    return render_template('index.html')

# Ruta pública de la política de privacidad (requerida por Meta)
@app.route('/privacidad')
def privacidad():
    return render_template('privacidad.html')

estado_usuario = {}

# Guarda el último mensaje que el bot le envió a cada número, para poder
# explicarlo "más fácil" si la persona lo pide (ver es_peticion_explicar_mas_facil
# y _explicar_mas_facil más abajo).
_ultimo_mensaje_bot = {}

# =========================================
# Protección contra mensajes duplicados
# =========================================
# WhatsApp reenvía el mismo mensaje (mismo "id") si no recibe una respuesta
# 200 de nuestro webhook lo suficientemente rápido. Esto pasa sobre todo
# cuando el servicio estuvo inactivo (por ejemplo, en el plan gratuito de
# Render, que "duerme" tras un rato sin uso) y tarda varios segundos en
# despertar para atender la primera petición. Sin esta protección, ese
# reenvío hace que el bot procese el mismo mensaje dos veces y responda
# el menú (o cualquier otra respuesta) por duplicado.
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

MENSAJE_FRECUENCIA_JUBILACION = (
    "¿Con qué frecuencia vas a ahorrar para tu retiro?\n"
    "1️⃣ Mensual\n"
    "2️⃣ Quincenal (cada 15 días)\n"
    "3️⃣ Catorcenal (cada 14 días)\n"
    "4️⃣ Semanal\n"
    "5️⃣ Otra frecuencia (tú me dices cuántas veces al año)"
)

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
# Jubilación: meta de ahorro para el retiro
# =========================================
def calcular_ahorro_jubilacion(meta, ahorro_actual, anios, tasa_anual_pct, periodos_por_anio, frecuencia_label):
    """
    Dado cuánto quiere tener una persona ahorrado para su retiro, cuánto tiene
    ya ahorrado para ese fin, un rendimiento anual esperado, un plazo y una
    frecuencia de aportación, calcula cuánto necesita aportar en cada periodo
    para llegar a su meta. A diferencia de Ahorro (que es un cálculo simple,
    sin intereses), aquí SÍ se asume un rendimiento compuesto sobre el dinero
    que ya tiene y sobre lo que va aportando, igual que en Inversión, pero
    resolviendo la aportación necesaria en vez de calcular el resultado final.

    Nota: esto NO es un estimador oficial de pensión del IMSS/ISSSTE ni de
    ninguna Afore; es una calculadora de meta de ahorro para el retiro,
    pensada solo con fines educativos.
    """
    try:
        meta = Decimal(str(meta))
        ahorro_actual = Decimal(str(ahorro_actual))
        tasa_anual_pct = Decimal(str(tasa_anual_pct))

        if meta <= 0:
            return (
                "Uy, algo no cuadró con esos datos 🤔 La meta debe ser mayor a cero. Escribe *menú* "
                "para empezar de nuevo."
            )
        if ahorro_actual < 0:
            return "Ese número no puede ser negativo 🙂 Si no tienes nada ahorrado todavía para tu retiro, escribe 0."
        if tasa_anual_pct < 0:
            return "La tasa de rendimiento esperada no puede ser negativa para este cálculo 🙂 Indica un número positivo (ejemplo: 8)."

        plazo, tasa_periodo = calcular_plazo_y_tasa_periodo(anios, tasa_anual_pct, periodos_por_anio)
        if plazo <= 0:
            return "El tiempo debe ser mayor a cero. Escribe *menú* para intentarlo de nuevo."

        fv_ahorro_actual = ahorro_actual * (Decimal("1") + tasa_periodo) ** plazo

        if fv_ahorro_actual >= meta:
            return (
                f"🎉 ¡Buena noticia! Si tu ahorro actual de ${ahorro_actual:,.2f} sigue generando un "
                f"rendimiento aproximado del {tasa_anual_pct}% anual, para dentro de {plazo} periodos "
                f"llegaría a unos ${fv_ahorro_actual.quantize(Decimal('0.01')):,.2f}, lo cual ya alcanza "
                f"tu meta de ${meta:,.2f} sin necesidad de aportar más 🙌\n\n"
                "🔍 *Nota:* Esto asume que el rendimiento se mantiene constante todo el tiempo, lo cual no "
                "siempre pasa en la vida real. Revisa tu plan cada cierto tiempo para confirmar que sigue "
                "en curso.\n\n"
                "Escribe *menú* para volver al inicio."
            )

        monto_faltante_fv = meta - fv_ahorro_actual
        if tasa_periodo == 0:
            aporte_por_periodo = (monto_faltante_fv / Decimal(plazo)).quantize(Decimal("0.01"))
        else:
            factor_anualidad = ((Decimal("1") + tasa_periodo) ** plazo - Decimal("1")) / tasa_periodo
            aporte_por_periodo = (monto_faltante_fv / factor_anualidad).quantize(Decimal("0.01"))

        total_aportado = (ahorro_actual + aporte_por_periodo * Decimal(plazo)).quantize(Decimal("0.01"))
        rendimiento_generado = (meta - total_aportado).quantize(Decimal("0.01"))

        return (
            "📌 Resultado de tu plan para el retiro:\n"
            f"💰 Meta: ${meta:,.2f}\n"
            f"🏦 Ya tienes ahorrado para esto: ${ahorro_actual:,.2f}\n"
            f"📆 Tiempo: {plazo} periodos, ahorrando de forma {frecuencia_label}\n"
            f"📈 Rendimiento anual esperado: {tasa_anual_pct}%\n\n"
            f"✅ Necesitas aportar ${aporte_por_periodo:,.2f} en cada periodo para lograrlo.\n"
            f"🧮 De ese total, aproximadamente ${total_aportado:,.2f} saldría de tu bolsillo y "
            f"${rendimiento_generado:,.2f} vendría del rendimiento generado con el tiempo.\n\n"
            "🔍 *Nota:* Este cálculo asume un rendimiento constante durante todo el plazo, lo cual no "
            "siempre pasa en la vida real, y es solo una calculadora de meta de ahorro, no un "
            "estimador oficial de tu pensión del IMSS, ISSSTE ni de tu Afore. Úsalo como referencia para "
            "planear, no como una cifra garantizada.\n\n"
            "Escribe *menú* para volver al inicio."
        )
    except Exception as e:
        return f"❌ Error al calcular: {e}"

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
    "5️⃣ ¿Quiénes hicimos este bot?\n"
    "6️⃣ Glosario de términos financieros\n"
    "7️⃣ Evalúa tu salud financiera\n"
    "8️⃣ Género y finanzas\n"
    "No te preocupes si no conoces todos estos términos, yo te voy guiando paso a paso 😊\n\n"
    "🔒 Este bot nunca te va a pedir contraseñas, NIP, CVV de tu tarjeta ni códigos de verificación. "
    "Si alguien más te los pide haciéndose pasar por este bot, no se los compartas."
)

mensaje_submenu_ahorro = (
    "💰 *Ahorro*\n\n"
    "1️⃣ ¿Cuánto debo apartar para lograr mi meta de ahorro?\n"
    "2️⃣ Consejos para ahorrar sin sufrir en el intento\n"
    "3️⃣ ¿Dónde puedo comparar cuentas de ahorro entre bancos?\n\n"
    "Escribe el número, o *menú* para regresar."
)

mensaje_submenu_credito = (
    "💳 *Crédito*\n\n"
    "1️⃣ Simular un crédito\n"
    "2️⃣ Ahorro con pagos extra a un crédito\n"
    "3️⃣ Costo real de compras a meses\n"
    "4️⃣ ¿Cuánto me pueden prestar?\n"
    "5️⃣ Consejos para pagar sin ahogarte\n"
    "6️⃣ Identificar un crédito caro\n"
    "7️⃣ Errores comunes al pedir crédito\n"
    "8️⃣ Entender el Buró de Crédito\n"
    "9️⃣ Tus derechos frente al cobro de deudas\n\n"
    "Escribe el número, o *menú* para regresar."
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
    "1️⃣ ¿Cuánto debo ahorrar para mi retiro?\n"
    "2️⃣ ¿Qué es una Afore y cómo saber en cuál estoy?\n"
    "3️⃣ ¿Cómo se calcula mi pensión? Ley 73 vs. Ley 97\n"
    "4️⃣ Aportaciones voluntarias: cómo aumentar tu ahorro para el retiro\n"
    "5️⃣ ¿Qué pasa si cambio de trabajo o dejo de cotizar?\n"
    "6️⃣ No he trabajado de forma formal, ¿aún así puedo ahorrar para mi retiro?\n\n"
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
# Basado en el instrumento "Semáforo de Salud Financiera" de la UABC.
# Cada dimensión se evalúa por separado (la persona elige cuál/es), pregunta
# por pregunta, con respuestas en escala de 1 a 5. Al final de cada
# dimensión se suman los puntos y se ubica el resultado en su rango
# correspondiente (🔴/🟡/🟢), con una recomendación conectada al resto del
# bot.
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
    "3️⃣ Seguridad (14 preguntas)\n"
    "4️⃣ Control (14 preguntas)\n"
    "5️⃣ Las 4 dimensiones completas (38 preguntas)\n\n"
    "Escribe el número, o *menú* para regresar."
)

DIMENSIONES_SALUD = {
    "resiliencia": {
        "nombre": "Resiliencia financiera",
        "emoji": "🛡️",
        "preguntas": [
            "Tengo suficiente dinero para asegurar que nunca falte comida en mi hogar.",
            "Tengo suficiente dinero para cubrir los gastos médicos necesarios para mí o mi familia.",
            "Puedo gastar dinero en pequeñas compras o regalos (boda, cumpleaños, otra ocasión, etc.) sin afectar mis finanzas.",
            "Puedo hacer frente a un gasto imprevisto importante sin comprometer mi estabilidad financiera.",
            "Puedo obtener rápidamente el dinero necesario para cubrir una emergencia financiera importante.",
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
            "En los últimos 12 meses, he podido realizar una compra grande (casa, terreno, vehículo, etc.) sin comprometer mi estabilidad financiera.",
            "Me propongo metas financieras claras sobre lo que quiero lograr con mi dinero.",
            "Tengo un plan de acción claro con pasos detallados para alcanzar mis metas financieras.",
            "Me siento confiado(a) de poder alcanzar cualquier meta financiera personal que me proponga.",
            "Puedo disfrutar la vida de la manera que quiero gracias a la forma en que gestiono mi dinero.",
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
            "En un mes típico, puedo pagar todos mis gastos y facturas.",
            "Puedo pagar el lugar donde vivo (hipoteca, renta, etc.) sin comprometer mi estabilidad financiera.",
            "Tengo una cuenta bancaria donde puedo ahorrar y recibir pagos sin problema.",
            "Ahorro de manera regular, apartando dinero cada mes.",
            "Tengo ahorros suficientes para cubrir varios meses de gastos en caso de necesidad.",
            "Mi historial crediticio es excelente y refleja una gestión financiera óptima.",
            "Tengo el nivel adecuado de deuda que no afecta mi estabilidad financiera.",
            "Pago siempre lo que debo en el tiempo adecuado cuando pido dinero prestado o realizo una compra a crédito.",
            "No necesito pedir dinero prestado para pagar mis deudas.",
            "Si necesitara pedir dinero prestado, podría obtenerlo fácilmente de diversas fuentes sin problemas.",
            "Tengo un plan financiero sólido para mi retiro.",
            "Tengo un seguro de vida.",
            "Tengo un seguro que protege mis propiedades, acciones e inversiones.",
            "Tengo un seguro médico.",
        ],
        "rangos": [
            (14, 37, "🔴", "Baja seguridad financiera",
             "Tienes dificultades significativas para manejar tus finanzas de manera segura. Podrías tener "
             "problemas para cumplir con tus obligaciones financieras, gestionar deudas, o ahorrar para el "
             "futuro, lo que te deja vulnerable ante imprevistos."),
            (38, 55, "🟡", "Seguridad financiera moderada",
             "Tienes una seguridad financiera moderada. Estás gestionando tus finanzas relativamente bien, pero "
             "hay áreas que necesitan mejora. Eres capaz de cubrir tus obligaciones financieras básicas, pero "
             "podrías estar en riesgo si enfrentas situaciones inesperadas."),
            (56, 70, "🟢", "Alta seguridad financiera",
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
            "Llevo un registro o control de mis gastos.",
            "Reviso regularmente mis estados de cuenta.",
            "Soy capaz de ahorrar regularmente una parte de mis ingresos para el futuro.",
            "Siempre tomo el tiempo necesario para decidir sobre pedir dinero prestado o hacer compras a crédito.",
            "No compro cosas por impulso de las que luego me arrepiento.",
            "Comprendo cómo el aumento en las tasas de interés afecta los precios de bienes y servicios.",
            "Una hipoteca de 15 años requiere pagos mensuales más altos, pero paga menos intereses a lo largo de la vida del préstamo en comparación con una hipoteca de 30 años.",
            "Si la inflación es mayor que la tasa de interés en mi cuenta de ahorro, podré comprar menos con ese dinero después de un año.",
            "Soy consciente de los riesgos y beneficios de diversificar mis inversiones para maximizar las ganancias y reducir pérdidas.",
            "Entiendo el impacto de hacer solo los pagos mínimos en mi deuda de tarjeta de crédito y cómo afecta el tiempo necesario para pagarla.",
            "Sé cuándo necesito asesoramiento sobre cómo manejar mi dinero.",
            "Sé dónde buscar asesoramiento para tomar decisiones financieras.",
            "Tengo metas financieras claras a corto y largo plazo.",
        ],
        "rangos": [
            (14, 37, "🔴", "Bajo control financiero",
             "Tienes un bajo nivel de control sobre tus finanzas. Podrías no estar revisando tus ingresos y "
             "gastos de manera regular, tener dificultades para cumplir con un presupuesto, y ser propenso/a a "
             "realizar compras impulsivas o tomar malas decisiones financieras."),
            (38, 55, "🟡", "Control financiero moderado",
             "Tienes un control financiero aceptable pero con áreas de mejora. Aunque eres capaz de gestionar "
             "tus finanzas en cierta medida, puede haber ocasiones en las que pierdas el control de tus gastos o "
             "no sigas estrictamente un plan financiero."),
            (56, 70, "🟢", "Alto control financiero",
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
    if primera:
        escala = (
            "Responde cada afirmación con un número del 1 al 5:\n"
            "1️⃣ Completamente en desacuerdo\n"
            "2️⃣ En desacuerdo\n"
            "3️⃣ Ni de acuerdo ni en desacuerdo\n"
            "4️⃣ De acuerdo\n"
            "5️⃣ Completamente de acuerdo"
        )
    else:
        escala = "Responde del 1 (completamente en desacuerdo) al 5 (completamente de acuerdo)."
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
    "💡 No tiene que ser exacta, pero te da un punto de partida si no sabes por dónde empezar.\n"
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

GLOSARIO_TERMINOS = [
    (["cat", "costo anual total"], "CAT (Costo Anual Total)",
     "Un número que junta la tasa de interés más las comisiones de un crédito, para que puedas comparar qué tan caro es de verdad. Entre más alto el CAT, más caro te sale el crédito."),
    (["tasa de interés", "tasa anual", "tasa periodo", "tasa por periodo"], "Tasa de interés",
     "El porcentaje que te cobran (si pides prestado) o que te pagan (si ahorras/inviertes) sobre el dinero, normalmente expresado por año."),
    (["deuda revolvente"], "Deuda revolvente",
     "Una deuda sin fecha fija para terminarse, como una tarjeta de crédito: vas pagando lo que usas cada mes, y puedes seguir usando el crédito disponible."),
    (["buró de crédito", "buro de credito"], "Buró de Crédito",
     "Una empresa que guarda tu historial de pagos de créditos. Si pagas bien, tu historial ayuda a que te aprueben créditos en el futuro; si te atrasas, se refleja ahí."),
    (["afore"], "Afore",
     "La institución que administra el dinero que se va acumulando para tu pensión (Administradora de Fondos para el Retiro)."),
    (["ley 73"], "Ley 73",
     "Las reglas para calcular la pensión de quienes se registraron en el IMSS ANTES del 1 de julio de 1997."),
    (["ley 97"], "Ley 97",
     "Las reglas para calcular la pensión de quienes se registraron en el IMSS A PARTIR del 1 de julio de 1997."),
    (["cuota social"], "Cuota social",
     "Una aportación extra que da el gobierno a tu cuenta Afore, además de lo que aportas tú y tu patrón."),
    (["semanas cotizadas"], "Semanas cotizadas",
     "El número de semanas que has trabajado de forma formal (registrado en el IMSS). Se necesita un mínimo de semanas cotizadas para tener derecho a una pensión."),
    (["interés compuesto"], "Interés compuesto",
     "Cuando el interés que ganas (o debes) también genera más interés con el tiempo, no solo el dinero original. Por eso el dinero puede crecer mucho más mientras más tiempo lo dejes invertido."),
    (["modalidad 40"], "Modalidad 40",
     "Una opción para seguir aportando al IMSS de forma voluntaria cerca de tu retiro (solo aplica si estás en Ley 73), para intentar subir el monto de tu pensión."),
    (["uma"], "UMA (Unidad de Medida y Actualización)",
     "Un valor en pesos que el gobierno actualiza cada año, y que se usa como referencia para calcular distintos límites y montos en trámites oficiales, incluyendo temas de pensiones."),
    (["cetes"], "CETES",
     "Certificados de la Tesorería: deuda del gobierno mexicano. Al comprarlos, básicamente le prestas dinero al gobierno a cambio de un interés. Se consideran de bajo riesgo."),
    (["diversificar", "diversificación"], "Diversificar",
     "No poner todo tu dinero en una sola opción de inversión, para que si una no funciona bien, no pierdas todo."),
    (["ingreso neto", "ingreso mensual neto"], "Ingreso neto",
     "Lo que realmente recibes de dinero después de impuestos: lo que te depositan o te dan en efectivo."),
    (["capacidad de pago"], "Capacidad de pago",
     "Cuánto dinero de tu ingreso te queda disponible cada mes, después de tus gastos y deudas actuales, para poder pagar un crédito nuevo sin ahogarte."),
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
    "Autora y creadora de este bot.\n"
    "________________________________________\n"
    "🌟 Dra. Sósima Carrillo\n"
    "Coautora de este proyecto, Líder del Cuerpo Académico Gestión Financiera y Administrativa de las "
    "Organizaciones. Su mentoría y su compromiso genuino con la educación financiera han sido una "
    "inspiración fundamental para que este proyecto exista.\n"
    "________________________________________\n"
    "🤝 Dras. Yésica Lizbet Benítez Niebla, Paulina Villalobos Torres y Zyanya María Villa Zamorano\n"
    "Coautoras de este proyecto, integrantes del Cuerpo Académico Gestión Disruptiva, Cooperación e "
    "Inclusión en Organizaciones y Comunidades. Su entusiasmo, compromiso y empeño en construir "
    "siempre ideas disruptivas y diferentes son parte esencial de la misión que compartimos: "
    "contribuir, desde nuestro trabajo, a cambiar al mundo.\n"
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

def enviar_mensaje(numero, texto):
    numero = normalizar_numero(numero)
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

def _procesar_mensaje_interno(mensaje, numero):
    texto_limpio = _BORDE_PUNTUACION_RE.sub('', mensaje).lower()

    # Evitar menú si estamos en pasos críticos
    subflujo_critico = False
    if numero in estado_usuario:
        esperando = estado_usuario[numero].get("esperando")
        if esperando in [
            "desde_cuando1", "desde2",
            "abono_extra1", "abono_extra2",
            "riesgo", "subopcion_prestamo",
            "submenu_despues_de_maximo",
            # Pasos nuevos de tasa anual / años / frecuencia de pago:
            # sus respuestas (números del 1 al 8, o años como 1-8) no deben
            # confundirse con los accesos directos del menú principal.
            "tasa_anual_credito", "anios_credito", "frecuencia_credito", "frecuencia_otro_credito",
            "tasa_anual2", "anios2", "frecuencia2", "frecuencia_otro2",
            "tasa_anual_simular", "anios_simular", "frecuencia_simular", "frecuencia_otro_simular",
            "tasa_anual_deseada", "anios_deseado", "frecuencia_deseada", "frecuencia_otro_deseada",
            # Submenús de la nueva estructura (Ahorro / Crédito) y pasos de la
            # calculadora de meta de ahorro: sus respuestas numéricas tampoco
            # deben confundirse con los accesos directos del menú principal.
            "menu_ahorro", "menu_credito", "menu_inversion", "menu_jubilacion",
            "ahorro_meta", "ahorro_inicial", "ahorro_tiempo_numero", "ahorro_tiempo_unidad",
            "ahorro_frecuencia", "ahorro_frecuencia_otro",
            "inversion_monto_inicial", "inversion_aportacion", "inversion_tasa_anual",
            "inversion_tiempo_numero", "inversion_tiempo_unidad",
            "inversion_frecuencia", "inversion_frecuencia_otro",
            "jubilacion_meta", "jubilacion_ahorro_actual", "jubilacion_tasa_anual",
            "jubilacion_tiempo_numero", "jubilacion_tiempo_unidad",
            "jubilacion_frecuencia", "jubilacion_frecuencia_otro",
            "menu_salud", "salud_pregunta", "menu_genero",
        ]:
            subflujo_critico = True

    # ======================
    # MENÚ PRINCIPAL 1..8
    # ======================
    if not subflujo_critico:
        if texto_limpio in ["hola", "menu", "menú"]:
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

        if texto_limpio in ["5", "quiénes hicimos este bot", "¿quiénes hicimos este bot?", "quienes hicimos este bot"]:
            estado_usuario[numero] = {}
            return mensaje_creditos

        if texto_limpio in ["6", "glosario", "glosario de términos financieros", "glosario de terminos financieros"]:
            estado_usuario[numero] = {}
            return mensaje_glosario

        if texto_limpio in [
            "7", "evalúa tu salud financiera", "evalua tu salud financiera",
            "evaluar mi salud financiera", "salud financiera",
        ]:
            estado_usuario[numero] = {"esperando": "menu_salud"}
            return mensaje_submenu_salud

        if texto_limpio in ["8", "género y finanzas", "genero y finanzas"]:
            estado_usuario[numero] = {"esperando": "menu_genero"}
            return mensaje_submenu_genero

        # Accesos directos por nombre exacto de cada herramienta, para quien ya conoce el bot
        # y prefiere escribirlo directamente sin pasar por los submenús.
        if texto_limpio in ["simular un crédito", "simular crédito"]:
            estado_usuario[numero] = {"esperando": "monto_credito"}
            return "Perfecto. Para comenzar, dime el monto del crédito que deseas simular."

        if texto_limpio in ["ahorro con pagos extra", "ver cuánto me ahorro si doy pagos extra al crédito"]:
            estado_usuario[numero] = {"esperando": "monto2"}
            return "Para estimar tu ahorro con pagos extra, primero dime el Monto del crédito."

        if texto_limpio in ["costo real de compras a meses", "calcular el costo real de compras a pagos fijos en tiendas departamentales"]:
            estado_usuario[numero] = {"esperando": "precio_contado"}
            return (
                "Vamos a calcular el costo real de una compra a pagos fijos.\n"
                "Por favor dime lo siguiente:\n\n"
                "1️⃣ ¿Cuál es el precio de contado del producto? (ejemplo: 1800)"
            )

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
                "🟡 Opción 5: Consejos para pagar un crédito sin ahogarte\n"
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
            if texto_limpio in [
                "1", "cuánto debo ahorrar para mi retiro",
                "cuanto debo ahorrar para mi retiro",
            ]:
                contexto["esperando"] = "jubilacion_meta"
                return (
                    "🌅 Vamos a calcular cuánto necesitas ahorrar para tu retiro.\n\n"
                    "1️⃣ ¿Cuánto dinero te gustaría tener ahorrado para cuando te retires? (por ejemplo: 1500000)"
                )
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
            contexto["salud_puntajes"][dim_key] = contexto["salud_puntajes"].get(dim_key, 0) + valor
            contexto["salud_preg_idx"] += 1

            resultado_texto = ""
            dim_actual = DIMENSIONES_SALUD[dim_key]
            if contexto["salud_preg_idx"] >= len(dim_actual["preguntas"]):
                # Se completó esta dimensión: calculamos y mostramos su resultado.
                resultado_texto = _resultado_dimension_salud(dim_key, contexto["salud_puntajes"][dim_key]) + "\n\n"
                contexto["salud_dim_idx"] += 1
                contexto["salud_preg_idx"] = 0

                if contexto["salud_dim_idx"] >= len(contexto["salud_dimensiones"]):
                    # No quedan más dimensiones por evaluar: terminamos aquí.
                    estado_usuario.pop(numero, None)
                    return (
                        resultado_texto
                        + "Escribe *menú* para volver al inicio, o *evalúa tu salud financiera* para "
                        "hacerlo de nuevo."
                    )

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
                estado_usuario[numero] = {"esperando": "ingreso"}
                return (
                    "Vamos a calcular cuánto podrías solicitar como crédito, según tu capacidad de pago.\n\n"
                    "Primero necesito saber:\n"
                    "1️⃣ ¿Cuál es tu ingreso mensual neto? Es decir, lo que realmente recibes después de "
                    "impuestos: lo que te depositan o te dan en efectivo. (ejemplo: 15000)"
                )
            if texto_limpio == "8":
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
            if texto_limpio == "5":
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
            if texto_limpio == "6":
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
            if texto_limpio == "7":
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
            if texto_limpio == "9":
                return mensaje_credito_derechos_cobranza
            return "Por favor, elige un número del 1 al 9 del menú de Crédito, o escribe *menú* para regresar al inicio."

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
                estado_usuario.pop(numero, None)
                return resultado
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
                estado_usuario.pop(numero, None)
                return resultado
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
                estado_usuario.pop(numero, None)
                return resultado
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
                estado_usuario.pop(numero, None)
                return resultado
            except Exception:
                return "Por favor, indica un número de veces al año (ejemplo: 24)."

        # --- Jubilación: flujo de meta de ahorro para el retiro ---
        if contexto["esperando"] == "jubilacion_meta":
            try:
                contexto["jubilacion_meta"] = Decimal(mensaje.replace(",", ""))
                if contexto["jubilacion_meta"] <= 0:
                    return "La meta debe ser mayor a cero. ¿Cuánto dinero te gustaría tener ahorrado para tu retiro? (ejemplo: 1500000)"
                contexto["esperando"] = "jubilacion_ahorro_actual"
                return "2️⃣ ¿Ya tienes algo ahorrado hoy pensando en tu retiro? Si no tienes nada todavía, escribe 0. (por ejemplo: 50000)"
            except:
                return "Por favor, indica tu meta como un número (ejemplo: 1500000)."

        if contexto["esperando"] == "jubilacion_ahorro_actual":
            try:
                contexto["jubilacion_ahorro_actual"] = Decimal(mensaje.replace(",", ""))
                if contexto["jubilacion_ahorro_actual"] < 0:
                    return "Ese número no puede ser negativo 🙂 Si no tienes nada ahorrado todavía, escribe 0."
                contexto["esperando"] = "jubilacion_tasa_anual"
                return "3️⃣ ¿Qué tasa de rendimiento ANUAL esperas obtener sobre ese ahorro? (por ejemplo, si esperas un 8% anual, escribe 8)"
            except:
                return "Por favor, escribe solo un número (ejemplo: 50000, o 0 si no tienes nada ahorrado todavía)."

        if contexto["esperando"] == "jubilacion_tasa_anual":
            try:
                tasa_anual = Decimal(mensaje.replace(",", "").replace("%", ""))
                if tasa_anual < 0:
                    return "La tasa esperada no puede ser negativa para este cálculo 🙂 Indica un número positivo (ejemplo: 8)."
                contexto["jubilacion_tasa_anual"] = tasa_anual
                contexto["esperando"] = "jubilacion_tiempo_numero"
                return "4️⃣ ¿En cuánto tiempo te quieres retirar? Escribe solo el número (por ejemplo: 25)"
            except:
                return "Por favor, indica la tasa de rendimiento anual como un número (ejemplo: 8)."

        if contexto["esperando"] == "jubilacion_tiempo_numero":
            try:
                tiempo_numero = Decimal(mensaje.replace(",", ""))
                if tiempo_numero <= 0:
                    return "El tiempo debe ser mayor a cero. ¿En cuánto tiempo te quieres retirar? (ejemplo: 25)"
                contexto["jubilacion_tiempo_numero"] = tiempo_numero
                contexto["esperando"] = "jubilacion_tiempo_unidad"
                return (
                    "¿Ese número que diste fue en meses o en años?\n"
                    "1️⃣ Meses\n"
                    "2️⃣ Años"
                )
            except:
                return "Por favor, indica el tiempo como un número (ejemplo: 25)."

        if contexto["esperando"] == "jubilacion_tiempo_unidad":
            if texto_limpio not in ["1", "2", "meses", "años", "anos", "año", "ano"]:
                return "Por favor, elige 1 (Meses) o 2 (Años)."
            if texto_limpio in ["1", "meses"]:
                anios = contexto["jubilacion_tiempo_numero"] / Decimal("12")
            else:
                anios = contexto["jubilacion_tiempo_numero"]
            contexto["jubilacion_anios"] = anios
            contexto["esperando"] = "jubilacion_frecuencia"
            return MENSAJE_FRECUENCIA_JUBILACION

        if contexto["esperando"] == "jubilacion_frecuencia":
            if texto_limpio == "5":
                contexto["esperando"] = "jubilacion_frecuencia_otro"
                return "¿Cuántas veces al año en total ahorrarías para tu retiro? (ejemplo: 24)"
            if texto_limpio not in FRECUENCIAS_PAGO:
                return "Por favor, elige una opción del 1 al 5."
            try:
                frecuencia_label, periodos_por_anio = FRECUENCIAS_PAGO[texto_limpio]
                resultado = calcular_ahorro_jubilacion(
                    contexto["jubilacion_meta"],
                    contexto["jubilacion_ahorro_actual"],
                    contexto["jubilacion_anios"],
                    contexto["jubilacion_tasa_anual"],
                    periodos_por_anio,
                    frecuencia_label,
                )
                estado_usuario.pop(numero, None)
                return resultado
            except Exception:
                return "Hubo un error al calcular. Revisa tus datos e intenta de nuevo."

        if contexto["esperando"] == "jubilacion_frecuencia_otro":
            try:
                periodos_por_anio = Decimal(mensaje.strip())
                if periodos_por_anio <= 0:
                    return "El número de veces al año debe ser mayor a cero (ejemplo: 24)."
                resultado = calcular_ahorro_jubilacion(
                    contexto["jubilacion_meta"],
                    contexto["jubilacion_ahorro_actual"],
                    contexto["jubilacion_anios"],
                    contexto["jubilacion_tasa_anual"],
                    periodos_por_anio,
                    "personalizada",
                )
                estado_usuario.pop(numero, None)
                return resultado
            except Exception:
                return "Por favor, indica un número de veces al año (ejemplo: 24)."

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
                estado_usuario.pop(numero)
                return (
                    f"💸 Si pagaras este crédito sin hacer abonos extra, terminarías pagando ${float(total_sin):,.2f} en total.\n"
                    f"Pero si decides abonar ${float(contexto['abono']):,.2f} adicionales por periodo desde el periodo {desde}...\n"
                    f"✅ Terminarías de pagar en menos tiempo (¡te ahorras {pagos_menos} pagos!)\n"
                    f"💰 Pagarías ${float(total_con):,.2f} en total\n"
                    f"🧮 Y te ahorrarías ${float(ahorro):,.2f} solo en intereses.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
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
                estado_usuario.pop(numero)
                return (
                    f"💸 Si pagaras este crédito sin hacer abonos extra, terminarías pagando ${float(total_sin):,.2f} en total.\n\n"
                    f"Pero si decides abonar ${float(contexto['abono']):,.2f} adicionales por periodo desde el periodo {desde}...\n"
                    f"✅ Terminarías de pagar en menos tiempo (¡te ahorras {pagos_menos} pagos!)\n"
                    f"💰 Pagarías ${float(total_con):,.2f} en total\n"
                    f"🧮 Y te ahorrarías ${float(ahorro):,.2f} solo en intereses.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
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
                # Convertimos la entrada a entero
                numero_pagos = int(mensaje.strip())
                contexto["numero_pagos_tienda"] = numero_pagos

                # Cambiamos a un nuevo estado donde preguntamos cuántos periodos hay en 1 año
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
                contexto["periodos_anuales"] = periodos_anuales  # ✅ Se guarda en el contexto

                mensaje_resultado = calcular_costo_credito_tienda(
                    contexto["precio_contado"],
                    contexto["pago_fijo_tienda"],
                    contexto["numero_pagos_tienda"],
                    periodos_anuales
                )

                estado_usuario.pop(numero)
                return mensaje_resultado

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
                estado_usuario[numero] = {}
                return (
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
                estado_usuario.pop(numero)
                return resultado
            except Exception:
                return "Uy, algo no cuadró con esos datos 🤔 Revisa que hayas escrito solo números y vuelve a intentarlo, o escribe *menú* para empezar de nuevo."

        if contexto["esperando"] == "frecuencia_otro_deseada":
            try:
                periodos_por_anio = Decimal(mensaje.strip())
            except Exception:
                return "Por favor, indica un número de pagos al año (ejemplo: 24)."
            try:
                resultado = _resolver_frecuencia_deseado(contexto, "personalizada", periodos_por_anio)
                estado_usuario.pop(numero)
                return resultado
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

    # Si nada coincide y no hay ninguna conversación activa con este número
    # (es la primera vez que escribe, o ya terminó una consulta anterior),
    # le damos la bienvenida sin importar qué haya escrito exactamente,
    # así no depende de que adivine la palabra "hola" para empezar.
    if numero not in estado_usuario:
        estado_usuario[numero] = {}
        return saludo_inicial

    # Si sí hay una conversación activa pero no reconocimos la respuesta:
    return (
        "No entendí ese mensaje 🙏 Escribe *menú* para ver todas las opciones, o revisa que tu "
        "respuesta sea del tipo que te pedí (por ejemplo, solo números si te pedí una cantidad)."
    )

# =========================================
# "Explícamelo más fácil": simplifica los términos técnicos de la
# última respuesta del bot, sin interrumpir la conversación en curso.
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
        # Primera vez que este número nos escribe: todavía no le hemos
        # dicho nada que explicarle más fácil, así que lo recibimos normal.
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
    la piden después.
    """
    texto_limpio = _BORDE_PUNTUACION_RE.sub('', mensaje).lower()
    if es_peticion_explicar_mas_facil(texto_limpio):
        return _explicar_mas_facil(numero)

    respuesta = _procesar_mensaje_interno(mensaje, numero)
    _ultimo_mensaje_bot[numero] = respuesta
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
        print(json.dumps(data, indent=2))  # 👈 muestra todo bonito en logs

        try:
            mensaje = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
            numero = data['entry'][0]['changes'][0]['value']['messages'][0]['from']
            message_id = data['entry'][0]['changes'][0]['value']['messages'][0].get('id')
        except Exception as e:
            print("⚠️ No se pudo procesar el mensaje:", e)
            return "ok", 200

        # WhatsApp puede reenviar el mismo mensaje (mismo id) si no le
        # respondemos rápido, por ejemplo justo cuando el servicio estaba
        # dormido y está despertando. Si ya procesamos este id, lo
        # ignoramos para no responder por duplicado.
        if ya_fue_procesado(message_id):
            print(f"⚠️ Mensaje duplicado ignorado (id={message_id})")
            return {"status": "duplicado_ignorado"}, 200

        respuesta = procesar_mensaje(mensaje, numero)
        enviar_mensaje(numero, respuesta)

        return {
            "status": "success",
            "respuesta_bot": respuesta
        }, 200
