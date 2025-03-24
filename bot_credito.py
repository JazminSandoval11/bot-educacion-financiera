# =========================================
# Bot de WhatsApp: Educación Financiera para el Mundo
# Autora: Dra. Jazmín Sandoval
# Descripción: Bot educativo para temas de crédito
# =========================================

from flask import Flask, request
import json
from decimal import Decimal, getcontext
import re  # Para eliminar caracteres invisibles

app = Flask(__name__)
getcontext().prec = 17  # Precisión tipo Excel

# Estado global de usuarios
estado_usuario = {}

# =========================================
# Función auxiliar para parsear a entero
# =========================================
def parsear_a_entero(texto: str) -> int:
    """
    Elimina espacios raros, caracteres invisibles,
    reemplaza comas por puntos, y convierte a int.
    """
    print(f"DEBUG-> parsear_a_entero recibió: {repr(texto)}")
    # Eliminar caracteres invisibles
    texto_limpio = re.sub(r"[\u200B-\u200D\uFEFF]", "", texto)
    # Quitar espacios y reemplazar comas
    texto_limpio = texto_limpio.strip().replace(",", ".")
    # Convertir primero a float, luego a int
    return int(float(texto_limpio))

# =========================================
# Cálculo de pago fijo (tipo Excel)
# =========================================
from math import log

def calcular_pago_fijo_excel(monto, tasa, plazo):
    """
    Calcula el pago fijo por periodo, tipo Excel (función PAGO).
    Monto, tasa y plazo deben ser floats o Decimals.
    """
    from decimal import Decimal
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
# Cálculo del ahorro con abonos extra
# =========================================
def calcular_ahorro_por_abonos(monto, tasa, plazo, abono_extra, desde_periodo):
    """
    Dado un crédito con pagos fijos,
    calcula cómo cambia si el usuario abona extra a partir de cierto periodo.
    """
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
    total_con_abonos = Decimal('0.00')

    while saldo > 0:
        interes = saldo * r
        abono_a_capital = pago_fijo - interes

        # A partir de 'desde', sumamos el abono_extra
        if periodo >= desde:
            abono_a_capital += abono
            total_pago_periodo = pago_fijo + abono
        else:
            total_pago_periodo = pago_fijo

        # Si alcanza para liquidar
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
# con iteración para encontrar la tasa efectiva por periodo
# =========================================
def calcular_costo_credito_tienda(precio_contado, pago_periodico, num_pagos, periodos_anuales):
    """
    Calcula el costo real de una compra a crédito con pagos fijos.

    Args:
        precio_contado (float): Precio de contado del producto.
        pago_periodico (float): Monto del pago periódico.
        num_pagos (int): Número total de pagos.
        periodos_anuales (int): Periodicidad anual (12=mensual, 52=semanal, etc.)

    Returns:
        (total_pagado, intereses, tasa_por_periodo_%, tasa_anual_equivalente_%)
    """
    from decimal import Decimal
    precio = Decimal(str(precio_contado))
    cuota = Decimal(str(pago_periodico))
    n = int(num_pagos)

    if precio <= 0 or cuota <= 0 or n <= 0 or periodos_anuales <= 0:
        raise ValueError("Todos los valores deben ser mayores a cero")

    # Iteración para encontrar r_estimada
    r_estimada = Decimal('0.05')
    for _ in range(100):
        try:
            base = (Decimal('1') + r_estimada) ** (-n)
            pago_calculado = precio * r_estimada / (1 - base)
            diferencia = pago_calculado - cuota
            if abs(diferencia) < Decimal('0.0001'):
                break
            r_estimada -= diferencia / 1000
        except:
            break

    tasa_periodo = r_estimada
    total_pagado = cuota * n
    intereses = total_pagado - precio

    # Tasa efectiva anual
    tasa_anual = ((Decimal('1') + tasa_periodo) ** Decimal(str(periodos_anuales))) - Decimal('1')

    return (
        total_pagado.quantize(Decimal("0.01")),
        intereses.quantize(Decimal("0.01")),
        (tasa_periodo * 100).quantize(Decimal("0.01")),
        (tasa_anual * 100).quantize(Decimal("0.01"))
    )

# =========================================
# Menú principal
# =========================================
saludo_inicial = (
    "👋 Hola 😊, soy tu asistente virtual de Educación Financiera para el Mundo, "
    "creado por la Dra. Jazmín Sandoval.\n"
    "Estoy aquí para ayudarte a comprender mejor cómo funcionan los créditos y "
    "tomar decisiones informadas 💳📊\n\n"
    "¿Sobre qué aspecto del crédito necesitas ayuda hoy?\n"
    "Escríbeme el número o el nombre de alguna de estas opciones para empezar:\n\n"
    "1️⃣ Simular un crédito\n"
    "2️⃣ Ver cuánto ahorro si doy pagos extras a un crédito\n"
    "3️⃣ Calcular el costo real de compras a pagos fijos en tiendas departamentales\n"
    "4️⃣ ¿Cuánto me pueden prestar?\n"
    "5️⃣ Consejos para pagar un crédito sin ahogarte\n"
    "6️⃣ Cómo identificar un crédito caro\n"
    "7️⃣ Errores comunes al solicitar un crédito\n"
    "8️⃣ Entender el Buró de Crédito"
)

def enviar_mensaje(numero, texto):
    print(f"[Enviar a {numero}]: {texto}")

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if verify_token == "arrocito2024":
            return challenge
        return "Token inválido", 403

    if request.method == "POST":
        data = request.get_json()
        try:
            mensaje = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
            numero = data['entry'][0]['changes'][0]['value']['messages'][0]['from']
        except:
            # Si no hay mensaje, devolvemos ok
            return "ok", 200

        respuesta = procesar_mensaje(mensaje, numero)
        enviar_mensaje(numero, respuesta)
        return "ok", 200

def procesar_mensaje(mensaje, numero):
    texto_limpio = mensaje.strip().lower()

    global estado_usuario
    if numero not in estado_usuario:
        estado_usuario[numero] = {}

    contexto = estado_usuario[numero]
    esperando = contexto.get("esperando")

    # ==========================
    # MENÚ PRINCIPAL
    # ==========================
    if not esperando:
        # Si no estamos esperando nada (subflujo), ofrecemos menú principal
        if texto_limpio in ["hola", "menu", "menú"]:
            # Reseteamos el estado
            estado_usuario[numero] = {}
            return saludo_inicial

        if texto_limpio in ["1", "simular un crédito"]:
            estado_usuario[numero] = {"esperando": "monto_credito"}
            return "Perfecto. Para comenzar, dime el monto del crédito que deseas simular."

        if texto_limpio in ["2", "ver cuánto me ahorro si doy pagos extra al crédito"]:
            estado_usuario[numero] = {"esperando": "monto2"}
            return "Para estimar tu ahorro con pagos extra, primero dime el Monto del crédito."

        # === NUEVO FLUJO PARA LA OPCIÓN 3 (Compras a pagos fijos) ===
        if texto_limpio in ["3", "calcular el costo real de compras a pagos fijos en tiendas departamentales"]:
            estado_usuario[numero] = {"esperando": "precio_contado_tienda"}
            return (
                "Calcularemos el costo real de la compra a pagos fijos.\n"
                "Dime primero el PRECIO DE CONTADO del producto (ej. 2000)."
            )
        # ============================================================

        if texto_limpio in ["4", "¿cuánto me pueden prestar?"]:
            estado_usuario[numero] = {"esperando": "ingreso"}
            return (
                "Vamos a calcular cuánto podrías solicitar como crédito, según tu capacidad de pago.\n\n"
                "Primero necesito saber:\n"
                "1️⃣ ¿Cuál es tu ingreso neto mensual? (Después de impuestos y deducciones)"
            )

        if texto_limpio in ["5", "consejos para pagar un crédito sin ahogarte"]:
            return (
                "🟡 Opción 5: Consejos para pagar un crédito sin ahogarte\n"
                "Pagar un crédito no tiene que sentirse como una carga eterna. "
                "Aquí van algunos consejos sencillos para ayudarte a pagar con más "
                "tranquilidad y menos estrés:\n"
                "________________________________________\n"
                "✅ 1. Haz pagos anticipados cuando puedas\n"
                "📌 Aunque no sea obligatorio, abonar un poco más al capital "
                "te ahorra intereses y reduce el plazo.\n"
                "💡 Incluso $200 o $500 adicionales hacen una gran diferencia "
                "con el tiempo.\n"
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
                "📌 Saber cuánto entra y cuánto sale te ayuda a organizar tus pagos "
                "sin descuidar otras necesidades.\n"
                "💡 Apóyate en apps, papel o Excel, lo que te funcione.\n"
                "________________________________________\n"
                "✅ 5. Prioriza las deudas más caras\n"
                "📌 Si tienes varias, enfócate primero en las que tienen interés más alto, "
                "como tarjetas de crédito.\n"
                "________________________________________\n"
                "Escribe *menú* para volver."
            )

        if texto_limpio in ["6", "cómo identificar un crédito caro"]:
            return (
                "Muchas veces un crédito parece accesible… hasta que ves lo que "
                "terminas pagando. Aquí te doy algunas claves para detectar si un "
                "crédito es caro:\n\n"
                "🔍 1. CAT (Costo Anual Total)\n"
                "Es una medida que incluye la tasa de interés, comisiones y otros cargos.\n"
                "📌 Entre más alto el CAT, más caro te saldrá el crédito.\n"
                "💡 Compara el CAT entre diferentes instituciones, no solo la tasa.\n\n"
                "🔍 2. Comisiones escondidas\n"
                "Algunos créditos cobran por apertura, por manejo, por pagos tardíos o "
                "por pagos anticipados 😵\n"
                "📌 Lee siempre el contrato antes de firmar.\n\n"
                "🔍 3. Tasa de interés variable\n"
                "📌 Algunos créditos no tienen tasa fija, sino que pueden subir.\n"
                "💡 Revisa si tu tasa es fija o variable. "
                "Las variables pueden volverse muy caras si sube la inflación.\n\n"
                "🔍 4. Pago mensual bajo con plazo largo\n"
                "Parece atractivo, pero terminas pagando muchísimo más en intereses.\n\n"
                "❗ Si el crédito parece demasiado fácil o rápido, pero no entiendes bien "
                "cuánto vas a pagar en total... ¡es una señal de alerta!\n\n"
                "Escribe *menú* para volver."
            )

        if texto_limpio in ["7", "errores comunes al solicitar un crédito"]:
            return (
                "Solicitar un crédito es una gran responsabilidad. "
                "Aquí te comparto algunos errores comunes que muchas personas cometen… "
                "¡y cómo evitarlos!\n"
                "________________________________________\n"
                "❌ 1. No saber cuánto terminarás pagando en total\n"
                "Muchas personas solo se fijan en el pago mensual y no en el costo "
                "total del crédito.\n"
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

        if texto_limpio in ["8", "entender el buró de crédito"]:
            estado_usuario[numero] = {"esperando": "submenu_buro"}
            return (
                "El Buró de Crédito no es un enemigo, es solo un registro de cómo "
                "has manejado tus créditos. Y sí, puede ayudarte o perjudicarte "
                "según tu comportamiento.\n"
                "________________________________________\n"
                "📊 ¿Qué es el Buró de Crédito?\n"
                "Es una empresa que guarda tu historial de pagos.\n"
                "📌 Si pagas bien, tu historial será positivo.\n"
                "📌 Si te atrasas, se reflejará ahí.\n"
                "________________________________________\n"
                "💡 Tener historial no es malo.\n"
                "De hecho, si nunca has pedido un crédito, no aparecerás en Buró y "
                "eso puede dificultar que te aprueben uno.\n"
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
                "¿Te gustaría saber cómo mejorar tu historial crediticio "
                "o qué pasos tomar para subir tu puntaje?\n"
                "Responde *sí* o *no*."
            )

        # Si no coincide nada de lo anterior
        return "No entendí. Escribe *menú* para ver las opciones."

    # ==========================
    # LÓGICA DE ESTADOS DETALLADA
    # ==========================

    # ----------------------------------
    # Opción 1: Simular un crédito
    # ----------------------------------
    if esperando == "monto_credito":
        try:
            contexto["monto"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "plazo_credito"
            return "¿A cuántos pagos (periodos) lo piensas pagar?"
        except:
            return "Por favor, indica el monto como un número (ej. 100000)"

    if esperando == "plazo_credito":
        try:
            contexto["plazo"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "tasa_credito"
            return (
                "¿Cuál es la tasa de interés en el mismo periodo en que harás los pagos?\n"
                "Por ejemplo, si pagarás cada mes, la tasa debe ser mensual (0.025 para 2.5%)."
            )
        except:
            return "Por favor, indica el plazo como un número."

    if esperando == "tasa_credito":
        try:
            monto = contexto["monto"]
            plazo = contexto["plazo"]
            tasa = Decimal(mensaje.replace(",", ""))
            pago = calcular_pago_fijo_excel(monto, tasa, plazo)
            total_pagado = pago * plazo
            intereses = total_pagado - monto

            contexto["tasa"] = tasa
            contexto["pago_fijo"] = pago
            contexto["esperando"] = "ver_si_abonos1"

            return (
                f"✅ Tu pago por periodo sería de: ${pago}\n"
                f"💰 Pagarías en total: ${total_pagado.quantize(Decimal('0.01'))}\n"
                f"📉 De los cuales ${intereses.quantize(Decimal('0.01'))} serían intereses.\n\n"
                "¿Te gustaría ver cuánto podrías ahorrar si haces pagos extra a capital?\n"
                "Responde *sí* o *no*."
            )
        except:
            return "Por favor escribe la tasa como un número decimal. Ejemplo: 0.025"

    if esperando == "ver_si_abonos1":
        if texto_limpio == "sí":
            contexto["esperando"] = "abono_extra1"
            return "¿Cuánto deseas abonar extra por periodo? (Ejemplo: 500)"
        elif texto_limpio == "no":
            estado_usuario.pop(numero, None)
            return "Ok, regresamos al inicio. Escribe *menú* si deseas ver otras opciones."
        else:
            return "Por favor, responde *sí* o *no*."

    if esperando == "abono_extra1":
        try:
            contexto["abono"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "desde_cuando1"
            return "¿A partir de qué periodo comenzarás a abonar esa cantidad extra? (Ejemplo: 4)"
        except:
            return "Por favor, un número válido (ej: 500)"

    if esperando == "desde_cuando1":
        try:
            desde = int(mensaje.strip())
            total_sin, total_con, ahorro, pagos_menos = calcular_ahorro_por_abonos(
                contexto["monto"], contexto["tasa"],
                contexto["plazo"], contexto["abono"], desde
            )
            estado_usuario.pop(numero, None)
            return (
                f"💸 Si pagaras este crédito sin hacer abonos extra, terminarías pagando ${total_sin} en total.\n\n"
                f"Pero si decides abonar ${contexto['abono']} adicionales por periodo desde el periodo {desde}...\n"
                f"✅ Terminarías de pagar en menos tiempo (¡te ahorras {pagos_menos} pagos!)\n"
                f"💰 Pagarías ${total_con} en total\n"
                f"🧮 Y te ahorrarías ${ahorro} solo en intereses.\n\n"
                "Escribe *menú* para volver al inicio."
            )
        except:
            return "Ocurrió un error al calcular el ahorro. Revisa tus datos."

    # ----------------------------------
    # Opción 2: Ver cuánto me ahorro con pagos extra
    # ----------------------------------
    if esperando == "monto2":
        try:
            contexto["monto"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "plazo2"
            return "¿A cuántos pagos (periodos) lo piensas pagar?"
        except:
            return "Por favor, indica el monto del crédito como un número."

    if esperando == "plazo2":
        try:
            contexto["plazo"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "tasa2"
            return "¿Cuál es la tasa de interés en el mismo periodo en que harás los pagos? (ej. 0.025 para 2.5%)"
        except:
            return "Por favor, indica el plazo como un número entero o decimal."

    if esperando == "tasa2":
        try:
            monto = contexto["monto"]
            plazo = contexto["plazo"]
            tasa = Decimal(mensaje.replace(",", ""))
            pago = calcular_pago_fijo_excel(monto, tasa, plazo)
            total_pagado = pago * plazo
            intereses = total_pagado - monto

            contexto["tasa"] = tasa
            contexto["pago_fijo"] = pago
            contexto["esperando"] = "abono_extra2"
            return (
                f"✅ Tu pago por periodo sería de: ${pago}\n"
                f"💰 Pagarías en total: ${total_pagado.quantize(Decimal('0.01'))}\n"
                f"📉 De los cuales ${intereses.quantize(Decimal('0.01'))} serían intereses.\n\n"
                "¿Cuánto deseas abonar extra por periodo? (Ejemplo: 500)"
            )
        except:
            return "Por favor escribe la tasa como un número decimal (ej. 0.025)."

    if esperando == "abono_extra2":
        try:
            contexto["abono"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "desde2"
            return "¿A partir de qué periodo comenzarás a abonar esa cantidad extra? (Ejemplo: 4)"
        except:
            return "Por favor, escribe solo la cantidad del abono extra (ejemplo: 500)"

    if esperando == "desde2":
        try:
            desde = int(mensaje.strip())
            total_sin, total_con, ahorro, pagos_menos = calcular_ahorro_por_abonos(
                contexto["monto"], contexto["tasa"],
                contexto["plazo"], contexto["abono"], desde
            )
            estado_usuario.pop(numero, None)
            return (
                f"💸 Si pagaras este crédito sin hacer abonos extra, terminarías pagando ${total_sin} en total.\n\n"
                f"Pero si decides abonar ${contexto['abono']} adicionales por periodo desde el periodo {desde}...\n"
                f"✅ Terminarías de pagar en menos tiempo (¡te ahorras {pagos_menos} pagos!)\n"
                f"💰 Pagarías ${total_con} en total\n"
                f"🧮 Y te ahorrarías ${ahorro} solo en intereses.\n\n"
                "Escribe *menú* para volver al inicio."
            )
        except:
            return "Ocurrió un error al calcular el ahorro. Revisa tus datos."

    # ----------------------------------
    # Opción 3 (Compras a pagos fijos) - NUEVO FLUJO
    # ----------------------------------
    # 1) Pedir precio contado
    if esperando == "precio_contado_tienda":
        try:
            contexto["precio_contado_tienda"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "numero_pagos_tienda"
            return "¿Cuántos pagos harás en total? (ej. 24)"
        except:
            return "Por favor, indica el precio de contado con un número."

    # 2) Pedir número de pagos
    if esperando == "numero_pagos_tienda":
        try:
            numero_pagos = parsear_a_entero(mensaje)
            contexto["numero_pagos_tienda"] = numero_pagos
            contexto["esperando"] = "pago_fijo_tienda"
            return "¿De cuánto será cada pago periódico? (ej. 250 por cada periodo)"
        except:
            return "Número de pagos inválido. Indica cuántos pagos harás (ej. 24)."

    # 3) Pedir pago fijo
    if esperando == "pago_fijo_tienda":
        try:
            pago_periodo = Decimal(mensaje.replace(",", ""))
            contexto["pago_fijo_tienda"] = pago_periodo
            contexto["esperando"] = "seleccionar_periodicidad_tienda"
            return (
                "Elige el tipo de periodicidad de tus pagos:\n"
                "1) Mensual (12 al año)\n"
                "2) Semanal (52 al año)\n"
                "3) Catorcenal (26 al año)\n"
                "4) Quincenal (24 al año)\n"
                "5) Mensual (12 al año)\n\n"
                "Escribe el número de la opción."
            )
        except:
            return "Cantidad inválida. Ejemplo: 250"

    # 4) Menu de periodicidades
    if esperando == "seleccionar_periodicidad_tienda":
        try:
            opcion = parsear_a_entero(mensaje)

            # Mapeo de las opciones
            if opcion == 1:
                periodos_anuales = 12  # Mensual
            elif opcion == 2:
                periodos_anuales = 52  # Semanal
            elif opcion == 3:
                periodos_anuales = 26  # Catorcenal
            elif opcion == 4:
                periodos_anuales = 24  # Quincenal
            elif opcion == 5:
                periodos_anuales = 12  # Mensual otra vez
            else:
                return (
                    "Opción inválida. Por favor, elige:\n"
                    "1) Mensual\n2) Semanal\n3) Catorcenal\n4) Quincenal\n5) Mensual"
                )

            precio = float(contexto["precio_contado_tienda"])
            cuota = float(contexto["pago_fijo_tienda"])
            n = int(contexto["numero_pagos_tienda"])

            total, intereses, tasa_periodo, tasa_anual = calcular_costo_credito_tienda(
                precio, cuota, n, periodos_anuales
            )

            estado_usuario.pop(numero, None)

            return (
                f"📊 Resultados:\n"
                f"💰 Precio de contado: ${precio}\n"
                f"📆 Pagos fijos de ${cuota} durante {n} periodos.\n\n"
                f"💸 Total pagado: ${total}\n"
                f"🧮 Intereses pagados: ${intereses}\n"
                f"📈 Tasa efectiva por periodo: {tasa_periodo}%\n"
                f"📅 Tasa anual equivalente (basado en {periodos_anuales} periodos al año): {tasa_anual}%\n\n"
                "Escribe *menú* para volver al inicio."
            )
        except:
            return (
                "Ocurrió un error. Asegúrate de elegir una opción válida (1-5). "
                "O escribe *menú* para volver al inicio."
            )

    # ----------------------------------
    # Opción 4 (capacidad de pago)
    # ----------------------------------
    if esperando == "ingreso":
        try:
            contexto["ingreso"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "pagos_fijos"
            return (
                "2️⃣ ¿Cuánto pagas mensualmente en créditos formales o instituciones financieras?\n"
                "(Es decir, en pagos de préstamos personales, hipotecas, "
                "crédito de auto, crédito de nómina, etc.)"
            )
        except:
            return "Por favor, escribe un número válido (ej: 12500)"

    if esperando == "pagos_fijos":
        try:
            contexto["pagos_fijos"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "deuda_revolvente"
            return "3️⃣ ¿Cuánto debes actualmente en tarjetas de crédito u otras deudas revolventes?"
        except:
            return "Por favor, indica la cantidad mensual que pagas en créditos (ej: 1800)"

    if esperando == "deuda_revolvente":
        try:
            contexto["deuda_revolvente"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "riesgo"
            return (
                "4️⃣ Según tu experiencia, ¿cómo calificarías tu nivel de riesgo como cliente?\n"
                "1. Bajo (siempre pago a tiempo)\n"
                "2. Medio (a veces me atraso)\n"
                "3. Alto (me atraso seguido o ya tengo deudas grandes)"
            )
        except:
            return "Por favor, indica un número para la deuda revolvente."

    if esperando == "riesgo":
        if texto_limpio not in ["1", "2", "3"]:
            return "Elige 1, 2 o 3 según tu nivel de riesgo."

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
            f"✅ Según tus datos, podrías pagar hasta ${capacidad_mensual} al mes en un nuevo crédito.\n\n"
            "¿Qué te gustaría hacer ahora?\n"
            "1. Calcular el monto máximo de crédito que podrías solicitar\n"
            "2. Validar si un crédito que te interesa podría ser aprobado\n"
            "Escribe 1 o 2 para continuar."
        )

    if esperando == "subopcion_prestamo":
        if texto_limpio == "1":
            contexto["esperando"] = "plazo_simular"
            return "📆 ¿A cuántos pagos (meses, quincenas, etc.) deseas simular el crédito?"
        elif texto_limpio == "2":
            contexto["esperando"] = "monto_credito_deseado"
            return "💰 ¿De cuánto sería el crédito que te interesa solicitar?"
        else:
            return "Por favor, escribe 1 o 2."

    if esperando == "plazo_simular":
        try:
            contexto["plazo_simular"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "tasa_simular"
            return "📈 ¿Cuál es la tasa de interés por periodo? (ej: 0.025 para 2.5%)"
        except:
            return "Número inválido."

    if esperando == "tasa_simular":
        try:
            tasa = Decimal(mensaje.replace(",", ""))
            plazo = contexto["plazo_simular"]
            capacidad = contexto["capacidad_mensual"]

            base = Decimal("1") + tasa
            potencia = base ** plazo
            inverso = Decimal("1") / potencia
            factor = (Decimal("1") - inverso) / tasa
            monto_maximo = (capacidad * factor).quantize(Decimal("0.01"))

            contexto["monto_maximo"] = monto_maximo
            contexto["esperando"] = "submenu_despues_de_maximo"

            return (
                f"✅ Con base en tu capacidad de pago de ${capacidad}, "
                f"podrías aspirar a un crédito de hasta ${monto_maximo}.\n\n"
                "¿Te gustaría ahora validar un crédito específico o volver al menú?\n"
                "1. Validar un crédito\n"
                "2. Regresar al menú\n"
                "Escribe 1 o 2."
            )
        except:
            return "Verifica tu tasa (ejemplo: 0.025)."

    if esperando == "submenu_despues_de_maximo":
        if texto_limpio == "1":
            contexto["esperando"] = "monto_credito_deseado"
            return "💰 ¿De cuánto sería el crédito que te interesa solicitar?"
        elif texto_limpio == "2":
            estado_usuario.pop(numero, None)
            return "Listo, escribe *menú* para ver más opciones."
        else:
            return "Por favor, escribe 1 o 2."

    if esperando == "monto_credito_deseado":
        try:
            contexto["monto_deseado"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "plazo_deseado"
            return "📆 ¿En cuántos pagos (meses, quincenas, etc.) planeas pagarlo?"
        except:
            return "Introduce un número válido."

    if esperando == "plazo_deseado":
        try:
            contexto["plazo_deseado"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "tasa_deseada"
            return "📈 ¿Cuál es la tasa de interés por periodo? (ej: 0.025)"
        except:
            return "Número inválido."

    if esperando == "tasa_deseada":
        try:
            monto = contexto["monto_deseado"]
            plazo = contexto["plazo_deseado"]
            tasa = Decimal(mensaje.replace(",", ""))
            capacidad = contexto["capacidad_mensual"]
            porcentaje_riesgo = contexto["porcentaje_riesgo"]

            pago_estimado = calcular_pago_fijo_excel(monto, tasa, plazo)
            if pago_estimado <= capacidad:
                estado_usuario.pop(numero, None)
                return (
                    f"✅ Puedes pagar este crédito sin problemas.\n"
                    f"Tu pago mensual estimado es ${pago_estimado}, dentro de tu capacidad (${capacidad}).\n"
                    "Escribe *menú* para volver."
                )
            else:
                diferencia = (pago_estimado - capacidad).quantize(Decimal("0.01"))
                incremento_ingreso = (diferencia / porcentaje_riesgo).quantize(Decimal("0.01"))
                reduccion_revolvente = (diferencia / Decimal("0.06")).quantize(Decimal("0.01"))
                estado_usuario.pop(numero, None)
                return (
                    f"❌ No podrías pagar este crédito.\n"
                    f"Pago mensual: ${pago_estimado} > tu capacidad: ${capacidad}.\n\n"
                    "🔧 Opciones:\n"
                    f"1. Reducir pagos fijos en al menos ${diferencia}.\n"
                    f"2. Aumentar ingresos en ~${incremento_ingreso}.\n"
                    f"3. Reducir deudas revolventes en ~${reduccion_revolvente}.\n\n"
                    "Escribe *menú* para volver."
                )
        except:
            return "Hubo un error. Revisa tus datos."

    # ----------------------------------
    # Opción 8: Submenú Buró
    # ----------------------------------
    if esperando == "submenu_buro":
        if texto_limpio == "sí":
            estado_usuario.pop(numero, None)
            return (
                "¿Cómo mejorar mi historial crediticio?\n"
                "Aquí tienes algunos consejos prácticos para mejorar tu score en Buró "
                "de Crédito y tener un historial más saludable 📈\n"
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
                "📌 Si pides varios préstamos en poco tiempo, parecerá que estás desesperado/a "
                "por dinero.\n"
                "✅ Ve uno a la vez y maneja bien el que tienes.\n"
                "________________________________________\n"
                "🔹 4. Usa algún crédito, aunque sea pequeño\n"
                "📌 Si no tienes historial, nunca tendrás score.\n"
                "✅ Una tarjeta departamental o un plan telefónico pueden ser un buen inicio "
                "si los manejas bien.\n"
                "________________________________________\n"
                "🔹 5. Revisa tu historial al menos una vez al año\n"
                "📌 Puedes pedir un reporte gratuito en www.burodecredito.com.mx\n"
                "✅ Asegúrate de que no haya errores y de que tus datos estén correctos.\n"
                "Escribe *menú*."
            )
        else:
            estado_usuario.pop(numero, None)
            return "Entiendo. Escribe *menú*."

    # Si no se cumplió ningún estado
    return "No entendí. Escribe *menú* para ver las opciones."

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if verify_token == "arrocito2024":
            return challenge
        return "Token inválido", 403

    if request.method == "POST":
        data = request.get_json()
        try:
            mensaje = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
            numero = data['entry'][0]['changes'][0]['value']['messages'][0]['from']
        except:
            return "ok", 200

        respuesta = procesar_mensaje(mensaje, numero)
        enviar_mensaje(numero, respuesta)
        return "ok", 200

# if __name__ == "__main__":
#     app.run(debug=True)
