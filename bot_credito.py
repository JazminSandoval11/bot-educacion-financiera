# =========================================
# Bot de WhatsApp: Educación Financiera para el Mundo
# Autora: Jazmín Sandoval
# Descripción: Bot educativo para temas de crédito
# =========================================
 
from flask import Flask, request, render_template
import json
import os
from decimal import Decimal, getcontext, ROUND_HALF_UP
from math import log
import requests  # <-- AÑADIDO
 
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
# Menú principal
# =========================================
saludo_inicial = (
    "👋 Hola 😊, soy tu asistente virtual de Educación Financiera para el Mundo, creado por la Dra. Jazmín Sandoval.\n"
    "Estoy aquí para ayudarte a comprender mejor cómo funcionan los créditos y tomar decisiones informadas 💳📊\n\n"
    "¿Sobre qué aspecto del crédito necesitas ayuda hoy?\n"
    "Escríbeme el número o el nombre de alguna de estas opciones para empezar:\n\n"
    "1️⃣ Simular un crédito\n"
    "2️⃣ Ver cuánto ahorro si doy pagos extras a un crédito\n"
    "3️⃣ Calcular el costo real de compras a pagos fijos en tiendas departamentales\n"
    "4️⃣ ¿Cuánto me pueden prestar?\n"
    "5️⃣ Consejos para pagar un crédito sin ahogarte\n"
    "6️⃣ Cómo identificar un crédito caro\n"
    "7️⃣ Errores comunes al solicitar un crédito\n"
    "8️⃣ Entender el Buró de Crédito\n\n"
    "No te preocupes si no conoces todos estos términos — yo te voy guiando paso a paso 😊"
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
 
def procesar_mensaje(mensaje, numero):
    texto_limpio = mensaje.strip().lower()
 
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
        ]:
            subflujo_critico = True
 
    # ======================
    # MENÚ PRINCIPAL 1..8
    # ======================
    if not subflujo_critico:
        if texto_limpio in ["hola", "menu", "menú"]:
            estado_usuario[numero] = {}
            return saludo_inicial
 
        if texto_limpio in ["1", "simular un crédito"]:
            estado_usuario[numero] = {"esperando": "monto_credito"}
            return "Perfecto. Para comenzar, dime el monto del crédito que deseas simular."
 
        if texto_limpio in ["2", "ver cuánto me ahorro si doy pagos extra al crédito"]:
            estado_usuario[numero] = {"esperando": "monto2"}
            return "Para estimar tu ahorro con pagos extra, primero dime el Monto del crédito."
 
        if texto_limpio in ["3", "calcular el costo real de compras a pagos fijos en tiendas departamentales"]:
            estado_usuario[numero] = {"esperando": "precio_contado"}
            return (
                "Vamos a calcular el costo real de una compra a pagos fijos.\n"
                "Por favor dime lo siguiente:\n\n"
                "1️⃣ ¿Cuál es el precio de contado del producto? (ejemplo: 1800)"
            )
 
        if texto_limpio in ["4", "¿cuánto me pueden prestar?"]:
            estado_usuario[numero] = {"esperando": "ingreso"}
            return (
                "Vamos a calcular cuánto podrías solicitar como crédito, según tu capacidad de pago.\n\n"
                "Primero necesito saber:\n"
                "1️⃣ ¿Cuál es tu ingreso mensual neto? Es decir, lo que realmente recibes después de "
                "impuestos — lo que te depositan o te dan en efectivo. (ejemplo: 15000)"
            )
 
        # Opción 5
        if texto_limpio in ["5", "consejos para pagar un crédito sin ahogarte"]:
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
 
        # Opción 6
        if texto_limpio in ["6", "cómo identificar un crédito caro"]:
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
 
        # Opción 7
        if texto_limpio in ["7", "errores comunes al solicitar un crédito"]:
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
 
        # Opción 8
        if texto_limpio in ["8", "entender el buró de crédito"]:
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
                    "No hay respuesta incorrecta — esto solo nos ayuda a calcular un número realista contigo."
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
 
    # Si nada coincide:
    return (
        "No entendí ese mensaje 🙏 Escribe *menú* para ver todas las opciones, o revisa que tu "
        "respuesta sea del tipo que te pedí (por ejemplo, solo números si te pedí una cantidad)."
    )
 
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
        except Exception as e:
            print("⚠️ No se pudo procesar el mensaje:", e)
            return "ok", 200
 
        respuesta = procesar_mensaje(mensaje, numero)
        enviar_mensaje(numero, respuesta)
 
        return {
            "status": "success",
            "respuesta_bot": respuesta
        }, 200
 
