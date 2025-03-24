# =========================================
# Bot de WhatsApp: Educación Financiera para el Mundo
# Autora: Dra. Jazmín Sandoval
# Descripción: Bot educativo para temas de crédito
# =========================================

from flask import Flask, request
import json
from decimal import Decimal, getcontext
from math import log

# =========================================
# Configuración general
# =========================================

app = Flask(__name__)
getcontext().prec = 17  # Precisión tipo Excel

estado_usuario = {}  # Diccionario para estados de usuarios

# =========================================
# Función: Cálculo de pago fijo (validado estilo Excel)
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
# Saludo inicial con menú principal
# =========================================

saludo_inicial = (
    "👋 Hola 😊, soy tu asistente virtual de Educación Financiera para el Mundo, creado por la Dra. Jazmín Sandoval.\n"
    "Estoy aquí para ayudarte a comprender mejor cómo funcionan los créditos y tomar decisiones informadas 💳📊\n\n"
    "¿Sobre qué aspecto del crédito necesitas ayuda hoy?\n"
    "Escríbeme el número o el nombre de alguna de estas opciones para empezar:\n\n"
    "1️⃣ Simular un crédito\n"
    "2️⃣ Ver cuánto me ahorro si doy pagos extra al crédito\n"
    "3️⃣ Calcular el costo real de compras a pagos fijos en tiendas\n"
    "4️⃣ ¿Cuánto me pueden prestar?\n"
    "5️⃣ Consejos para pagar un crédito sin ahogarte\n"
    "6️⃣ Cómo identificar un crédito caro\n"
    "7️⃣ Errores comunes al solicitar un crédito\n"
    "8️⃣ Entender el Buró de Crédito"
)

# =========================================
# Webhook para conexión con WhatsApp Cloud API
# =========================================

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if verify_token == "mi_token_secreto":
            return challenge
        return "Token inválido", 403

    if request.method == "POST":
        data = request.get_json()
        try:
            mensaje = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body'].strip().lower()
            numero = data['entry'][0]['changes'][0]['value']['messages'][0]['from']
        except:
            return "ok", 200

        respuesta = procesar_mensaje(mensaje, numero)
        enviar_mensaje(numero, respuesta)
        return "ok", 200

def enviar_mensaje(numero, texto):
    print(f"Enviando a {numero}: {texto}")
# =========================================
# Procesar mensajes - Parte de simulación (Opción 1)
# =========================================

estado_usuario = {}  # Seguimiento por usuario

def procesar_mensaje(mensaje, numero):
    if mensaje in ["hola", "menú", "menu"]:
        estado_usuario[numero] = {}
        return saludo_inicial

    if mensaje in ["1", "simular un crédito"]:
        estado_usuario[numero] = {"esperando": "monto_credito"}
        return "Perfecto. Para comenzar, dime el monto del crédito que deseas simular."

    if numero in estado_usuario and "esperando" in estado_usuario[numero]:
        contexto = estado_usuario[numero]

        if contexto["esperando"] == "monto_credito":
            contexto["monto"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "plazo_credito"
            return "¿A cuántos pagos (periodos) lo piensas pagar?"

        if contexto["esperando"] == "plazo_credito":
            contexto["plazo"] = Decimal(mensaje.replace(",", ""))
            contexto["esperando"] = "tasa_credito"
            return (
                "¿Cuál es la tasa de interés en el mismo periodo en que harás los pagos?\n"
                "📌 Por ejemplo, si pagarás cada mes, la tasa debe ser mensual."
            )

        if contexto["esperando"] == "tasa_credito":
            try:
                monto = contexto["monto"]
                plazo = contexto["plazo"]
                tasa = Decimal(mensaje.replace(",", ""))
                pago = calcular_pago_fijo_excel(monto, tasa, plazo)
                total_pagado = pago * plazo
                intereses = total_pagado - monto

                # Guardamos para opción de abonos extra
                contexto["tasa"] = tasa
                contexto["pago_fijo"] = pago
                contexto["esperando"] = "ver_si_abonos"

                return (
                    f"✅ Tu pago por periodo sería de: ${pago}\n"
                    f"💰 Pagarías en total: ${total_pagado.quantize(Decimal('0.01'))}\n"
                    f"📉 De los cuales ${intereses.quantize(Decimal('0.01'))} serían intereses.\n\n"
                    "¿Te gustaría ver cuánto podrías ahorrar si haces pagos extra a capital?\n"
                    "Responde *sí* o *no*."
                )
            except:
                return "Por favor escribe la tasa como un número decimal. Ejemplo: 0.025 para 2.5%"

        if contexto["esperando"] == "ver_si_abonos":
            if mensaje == "sí":
                contexto["esperando"] = "abono_extra"
                return "¿Cuánto deseas abonar extra por periodo? (Ejemplo: 500)"
            elif mensaje == "no":
                estado_usuario[numero] = {}
                return "Ok, pero si gustas reconsiderarlo porque realmente es algo útil, lo revisamos después 😊"
# ============================================================
# Cálculo del ahorro con abonos extra (ajuste de último pago)
# ============================================================
def calcular_ahorro_por_abonos(monto, tasa, plazo, abono_extra, desde_periodo):
    getcontext().prec = 17
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

    while saldo > 0:
        # Separamos estas líneas para evitar error de sintaxis
        interes = saldo * r
        abono_a_capital = pago_fijo - interes

        # Si ya toca aplicar el abono extra
        if periodo >= desde:
            abono_a_capital += abono

        # Si el abono a capital ya cubre el saldo, terminamos
        if abono_a_capital >= saldo:
            interes_final = saldo * r
            ultimo_pago = saldo + interes_final
            intereses_totales += interes_final
            pagos_realizados += 1
            break

        saldo -= abono_a_capital
        intereses_totales += interes
        pagos_realizados += 1
        periodo += 1

    total_sin_abonos = pago_fijo * n
    total_con_abonos = (pago_fijo * (pagos_realizados - 1)) + ultimo_pago
    ahorro_total = total_sin_abonos - total_con_abonos
    pagos_ahorrados = n - pagos_realizados

    return (
        total_sin_abonos.quantize(Decimal("0.01")),
        total_con_abonos.quantize(Decimal("0.01")),
        ahorro_total.quantize(Decimal("0.01")),
        pagos_ahorrados
    )


# ============================================
# Conectar flujo si el usuario quiere abonar
# ============================================
# Ojo: aquí asumo que este bloque debe estar FUERA de la función anterior,
# en el nivel superior (y con indentación de 0, como se ve).
if contexto["esperando"] == "abono_extra":
    try:
        contexto["abono"] = Decimal(mensaje.replace(",", ""))
        contexto["esperando"] = "desde_cuando"
        return "¿A partir de qué periodo comenzarás a abonar esa cantidad? (Ejemplo: 4)"
    except:
        return "Por favor, escribe solo el número del abono extra (ejemplo: 500)"


if contexto["esperando"] == "desde_cuando":
    try:
        desde = int(mensaje.strip())
        total_sin, total_con, ahorro, pagos_menos = calcular_ahorro_por_abonos(
            contexto["monto"],
            contexto["tasa"],
            contexto["plazo"],
            contexto["abono"],
            desde
        )
        estado_usuario.pop(numero)
        return (
            f"💸 Si pagaras este crédito sin hacer abonos extra, terminarías pagando ${total_sin} en total.\n\n"
            f"Pero si decides abonar ${contexto['abono']} adicionales por periodo desde el periodo {desde}...\n"
            f"✅ Terminarías de pagar en menos tiempo (¡te ahorras {pagos_menos} pagos!)\n"
            f"💰 Pagarías ${total_con} en total\n"
            f"🧮 Y te ahorrarías ${ahorro} solo en intereses"
        )
    except:
        return "Ocurrió un error al calcular el ahorro. Por favor revisa tus datos."
Revisa especialmente:
La línea interes = saldo * r debe estar separada de abono_a_capital = pago_fijo - interes.

La indentación de los if contexto["esperando"] ... al final, para que no estén dentro de la función (a menos que esa sea tu intención).

Usa la misma cantidad de espacios en toda la función o en todo el archivo (generalmente 4 espacios por nivel de indentación, o tabs, pero no mezclar).

Con esto, debería solucionarse el error IndentationError: unexpected indent y también el problema de sintaxis.







Tú dijiste:
puedes proporcionarme cómo debería quedar?
ChatGPT Plus
Razonado sobre el error de indentación durante 13 segundos
A continuación te dejo un ejemplo de cómo quedaría el código con la indentación correcta y separando las líneas que causaban error. Siéntete libre de ajustarlo a tu lógica/variables reales si tu archivo completo tiene más secciones.

Puntos importantes que corregimos:

Separar la línea que contenía interes = saldo * r abono_a_capital = ... en dos asignaciones independientes.

Alinear la indentación para que el bloque de if contexto["esperando"] == "abono_extra": esté fuera de la función calcular_ahorro_por_abonos().

python
Copiar
Editar
from decimal import Decimal, getcontext

# Suponiendo que existe una función para el pago fijo (ajusta la fórmula según tu caso real).
def calcular_pago_fijo_excel(P, r, n):
    # Ejemplo genérico (fórmula de anualidad):
    return (P * r) / (1 - (1 + r) ** -n)

# ============================================================
# Cálculo del ahorro con abonos extra (ajuste de último pago)
# ============================================================
def calcular_ahorro_por_abonos(monto, tasa, plazo, abono_extra, desde_periodo):
    getcontext().prec = 17
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

    while saldo > 0:
        # Separar en dos líneas para evitar error de sintaxis
        interes = saldo * r
        abono_a_capital = pago_fijo - interes

        # Si ya toca aplicar el abono extra
        if periodo >= desde:
            abono_a_capital += abono

        # Si el abono a capital cubre todo el saldo, se calcula el último pago
        if abono_a_capital >= saldo:
            interes_final = saldo * r
            ultimo_pago = saldo + interes_final
            intereses_totales += interes_final
            pagos_realizados += 1
            break

        saldo -= abono_a_capital
        intereses_totales += interes
        pagos_realizados += 1
        periodo += 1

    total_sin_abonos = pago_fijo * n
    total_con_abonos = (pago_fijo * (pagos_realizados - 1)) + ultimo_pago
    ahorro_total = total_sin_abonos - total_con_abonos
    pagos_ahorrados = n - pagos_realizados

    return (
        total_sin_abonos.quantize(Decimal("0.01")),
        total_con_abonos.quantize(Decimal("0.01")),
        ahorro_total.quantize(Decimal("0.01")),
        pagos_ahorrados
    )

# ============================================
# Conectar flujo si el usuario quiere abonar
# ============================================
# Este bloque debe estar al mismo nivel que la función anterior (no dentro de ella)
if contexto["esperando"] == "abono_extra":
    try:
        contexto["abono"] = Decimal(mensaje.replace(",", ""))
        contexto["esperando"] = "desde_cuando"
        return "¿A partir de qué periodo comenzarás a abonar esa cantidad? (Ejemplo: 4)"
    except:
        return "Por favor, escribe solo el número del abono extra (ejemplo: 500)"


if contexto["esperando"] == "desde_cuando":
    try:
        desde = int(mensaje.strip())
        total_sin, total_con, ahorro, pagos_menos = calcular_ahorro_por_abonos(
            contexto["monto"],
            contexto["tasa"],
            contexto["plazo"],
            contexto["abono"],
            desde
        )
        # Se asume que estado_usuario y numero están definidos y se usan para limpiar el estado
        estado_usuario.pop(numero)

        return (
            f"💸 Si pagaras este crédito sin hacer abonos extra, terminarías pagando ${total_sin} en total.\n\n"
            f"Pero si decides abonar ${contexto['abono']} adicionales por periodo desde el periodo {desde}...\n"
            f"✅ Terminarías de pagar en menos tiempo (¡te ahorras {pagos_menos} pagos!)\n"
            f"💰 Pagarías ${total_con} en total\n"
            f"🧮 Y te ahorrarías ${ahorro} solo en intereses"
        )
    except:
        return "Ocurrió un error al calcular el ahorro. Por favor revisa tus datos."
# ============================================================
# Cálculo del costo real de compras a pagos fijos en tiendas
# ============================================================

from math import log

def calcular_costo_credito_tienda(precio_contado, pago_periodico, num_pagos):
    precio = Decimal(str(precio_contado))
    cuota = Decimal(str(pago_periodico))
    n = int(num_pagos)

    saldo = precio
    r_estimada = Decimal('0.05')
    for _ in range(100):
        try:
            base = (Decimal('1') + r_estimada) ** (-n)
            pago_calculado = saldo * r_estimada / (1 - base)
            diferencia = pago_calculado - cuota
            if abs(diferencia) < Decimal('0.0001'):
                break
            r_estimada -= diferencia / 1000
        except:
            break

    tasa_periodo = r_estimada
    total_pagado = cuota * n
    intereses = total_pagado - precio
    tasa_anual = ((Decimal('1') + tasa_periodo) ** Decimal('12')) - Decimal('1')

    return (
        total_pagado.quantize(Decimal("0.01")),
        intereses.quantize(Decimal("0.01")),
        (tasa_periodo * 100).quantize(Decimal("0.01")),
        (tasa_anual * 100).quantize(Decimal("0.01"))
    )

# ============================================================
# Conectar flujo si el usuario elige la opción 3 del menú
# ============================================================

        if mensaje in ["3", "calcular el costo real de compras a pagos fijos en tiendas"]:
            estado_usuario[numero] = {"esperando": "precio_contado"}
            return (
                "Vamos a calcular el costo real de una compra a pagos fijos.\n"
                "Por favor dime lo siguiente:\n\n"
                "1️⃣ ¿Cuál es el precio de contado del producto?"
            )

        if contexto.get("esperando") == "precio_contado":
            try:
                contexto["precio_contado"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "pago_fijo"
                return "2️⃣ ¿De cuánto será cada pago (por ejemplo: 250)?"
            except:
                return "Por favor, indica el precio de contado con números (ejemplo: 1800)"

        if contexto.get("esperando") == "pago_fijo":
            try:
                contexto["pago_fijo"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "numero_pagos"
                return "3️⃣ ¿Cuántos pagos harás en total?"
            except:
                return "Por favor, escribe solo la cantidad del pago fijo (ejemplo: 250)"

        if contexto.get("esperando") == "numero_pagos":
            try:
                num_pagos = int(mensaje.strip())
                total, intereses, tasa_periodo, tasa_anual = calcular_costo_credito_tienda(
                    contexto["precio_contado"],
                    contexto["pago_fijo"],
                    num_pagos
                )
                estado_usuario.pop(numero)
                return (
                    f"📊 Aquí tienes los resultados:\n"
                    f"💰 Precio de contado: ${contexto['precio_contado']}\n"
                    f"📆 Pagos fijos de ${contexto['pago_fijo']} durante {num_pagos} periodos\n\n"
                    f"💸 Total pagado: ${total}\n"
                    f"🧮 Intereses pagados: ${intereses}\n"
                    f"📈 Tasa por periodo: {tasa_periodo}%\n"
                    f"📅 Tasa anual equivalente (aproximada): {tasa_anual}%"
                )
            except:
                return "Ocurrió un error al calcular el crédito. Revisa tus datos e intenta de nuevo."
# ============================================================
# Parte 6: ¿Cuánto me pueden prestar?
# ============================================================

        if mensaje in ["4", "¿cuánto me pueden prestar?"]:
            estado_usuario[numero] = {"esperando": "ingreso"}
            return (
                "Vamos a calcular cuánto podrías solicitar como crédito, con base en tu capacidad de pago.\n\n"
                "Primero necesito saber:\n"
                "1️⃣ ¿Cuál es tu ingreso neto mensual? (Después de impuestos y deducciones)"
            )

        if contexto.get("esperando") == "ingreso":
            try:
                contexto["ingreso"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "pagos_fijos"
                return (
                    "2️⃣ ¿Cuánto pagas mensualmente en créditos formales o instituciones financieras?\n"
                    "(No incluyas comida, renta, etc.)"
                )
            except:
                return "Por favor, escribe solo el ingreso mensual en números (ejemplo: 12500)"

        if contexto.get("esperando") == "pagos_fijos":
            try:
                contexto["pagos_fijos"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "deuda_revolvente"
                return (
                    "3️⃣ ¿Cuánto debes actualmente en tarjetas de crédito u otras deudas revolventes?"
                )
            except:
                return "Por favor, indica solo la cantidad mensual que pagas en créditos (ejemplo: 1800)"

        if contexto.get("esperando") == "deuda_revolvente":
            try:
                contexto["deuda_revolvente"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "riesgo"
                return (
                    "4️⃣ Según tu experiencia, ¿cómo calificarías tu nivel de riesgo como cliente?\n"
                    "Escribe el número que mejor te describa:\n"
                    "1. Bajo: Siempre pago a tiempo y mantengo buen control\n"
                    "2. Medio: A veces me atraso o uso mucho mis tarjetas\n"
                    "3. Alto: Me atraso seguido o ya tengo deudas grandes"
                )
            except:
                return "Por favor, indica el monto total que debes en tarjetas u otros créditos revolventes."

        if contexto.get("esperando") == "riesgo":
            riesgo = mensaje.strip()
            if riesgo not in ["1", "2", "3"]:
                return "Por favor, escribe 1, 2 o 3 para indicar tu nivel de riesgo."
            
            contexto["riesgo"] = riesgo
            porcentaje_riesgo = {"1": Decimal("0.60"), "2": Decimal("0.45"), "3": Decimal("0.30")}[riesgo]
            contexto["porcentaje_riesgo"] = porcentaje_riesgo

            ingreso = contexto["ingreso"]
            pagos_fijos = contexto["pagos_fijos"]
            deuda_revolvente = contexto["deuda_revolvente"]
            pago_estimado_revolvente = deuda_revolvente * Decimal("0.06")
            capacidad_total = ingreso * porcentaje_riesgo
            capacidad_mensual = capacidad_total - pagos_fijos - pago_estimado_revolvente
            capacidad_mensual = capacidad_mensual.quantize(Decimal("0.01"))

            contexto["capacidad_mensual"] = capacidad_mensual
            contexto["esperando"] = "subopcion_prestamo"

            return (
                f"✅ Según tus datos, podrías pagar hasta ${capacidad_mensual} al mes en un nuevo crédito.\n\n"
                "¿Qué te gustaría hacer ahora?\n"
                "1. Calcular el monto máximo de crédito que podrías solicitar\n"
                "2. Validar si un crédito que te interesa podría ser aprobado\n"
                "Escribe 1 o 2 para continuar."
            )

        if contexto.get("esperando") == "subopcion_prestamo":
            opcion = mensaje.strip()
            if opcion == "1":
                contexto["esperando"] = "plazo_simular"
                return "📆 ¿A cuántos pagos (meses, quincenas, etc.) deseas simular el crédito?"
            elif opcion == "2":
                contexto["esperando"] = "monto_credito_deseado"
                return "💰 ¿De cuánto sería el crédito que te interesa solicitar?"
            else:
                return "Por favor, escribe 1 para simular el monto máximo o 2 para validar un crédito que ya tienes en mente."

        if contexto.get("esperando") == "plazo_simular":
            try:
                contexto["plazo_simular"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa_simular"
                return "📈 ¿Cuál es la tasa de interés por periodo? Ejemplo: para 2.5%, escribe 0.025"
            except:
                return "Por favor, indica el plazo en cantidad de pagos (ejemplo: 24)"

        if contexto.get("esperando") == "tasa_simular":
            try:
                tasa = Decimal(mensaje.replace(",", ""))
                plazo = contexto["plazo_simular"]
                capacidad = contexto["capacidad_mensual"]
                base = Decimal("1") + tasa
                potencia = base ** plazo
                inverso = Decimal("1") / potencia
                factor = (Decimal("1") - inverso) / tasa
                monto_maximo = (capacidad * factor).quantize(Decimal("0.01"))
                estado_usuario.pop(numero)
                return (
                    f"✅ Con base en tu capacidad de pago de ${capacidad}, podrías aspirar a un crédito de hasta aproximadamente ${monto_maximo}.\n\n"
                    "¿Deseas volver al menú? Escribe *menú*."
                )
            except:
                return "Por favor asegúrate de indicar la tasa como número decimal (ejemplo: 0.025 para 2.5%)"

        if contexto.get("esperando") == "monto_credito_deseado":
            try:
                contexto["monto_deseado"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "plazo_deseado"
                return "📆 ¿En cuántos pagos (meses, quincenas, etc.) planeas pagarlo?"
            except:
                return "Por favor, escribe solo la cantidad del crédito deseado (ejemplo: 300000)"

        if contexto.get("esperando") == "plazo_deseado":
            try:
                contexto["plazo_deseado"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa_deseada"
                return "📈 ¿Cuál es la tasa de interés por periodo? Ejemplo: para 2.5%, escribe 0.025"
            except:
                return "Por favor, indica el número total de pagos."

        if contexto.get("esperando") == "tasa_deseada":
            try:
                monto = contexto["monto_deseado"]
                plazo = contexto["plazo_deseado"]
                tasa = Decimal(mensaje.replace(",", ""))
                capacidad = contexto["capacidad_mensual"]
                porcentaje_riesgo = contexto["porcentaje_riesgo"]

                pago_estimado = calcular_pago_fijo_excel(monto, tasa, plazo)

                if pago_estimado <= capacidad:
                    estado_usuario.pop(numero)
                    return (
                        f"✅ Buenas noticias: podrías pagar este crédito.\n"
                        f"Tu pago mensual estimado sería de ${pago_estimado}, lo cual está dentro de tu capacidad de pago mensual (${capacidad}).\n\n"
                        "¿Deseas volver al menú? Escribe *menú*."
                    )
                else:
                    diferencia = (pago_estimado - capacidad).quantize(Decimal("0.01"))
                    incremento_ingreso = (diferencia / porcentaje_riesgo).quantize(Decimal("0.01"))
                    reduccion_revolvente = (diferencia / Decimal("0.06")).quantize(Decimal("0.01"))
                    estado_usuario.pop(numero)
                    return (
                        f"❌ Actualmente no podrías pagar ese crédito.\n"
                        f"El pago mensual estimado sería de ${pago_estimado}, pero tu capacidad máxima es de ${capacidad}.\n\n"
                        "🔧 Algunas alternativas para hacerlo viable:\n"
                        f"1. Reducir tus pagos fijos en al menos ${diferencia}.\n"
                        f"2. Aumentar tus ingresos mensuales en aproximadamente ${incremento_ingreso}.\n"
                        f"3. Pagar tus deudas revolventes (como tarjetas) en al menos ${reduccion_revolvente}.\n\n"
                        "¿Deseas volver al menú? Escribe *menú*."
                    )
            except:
                return "Ocurrió un error al validar el crédito. Revisa tus datos y vuelve a intentarlo."
        if mensaje in ["5", "consejos para pagar un crédito sin ahogarte"]:
            return (
                "🟡 *Consejos para pagar un crédito sin ahogarte*\n"
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
                "¿Te gustaría simular cuánto podrías ahorrar si haces pagos extra?\n"
                "Solo dime *simular crédito* o escribe *menú* para regresar al inicio."
            )
        if mensaje in ["6", "cómo identificar un crédito caro"]:
            return (
                "🟡 *Cómo identificar un crédito caro*\n"
                "Muchas veces un crédito parece accesible… hasta que ves lo que terminas pagando. Aquí te doy algunas claves para detectar si un crédito es caro:\n"
                "________________________________________\n"
                "🔍 1. CAT (Costo Anual Total)\n"
                "Es una medida que incluye la tasa de interés, comisiones y otros cargos.\n"
                "📌 Entre más alto el CAT, más caro te saldrá el crédito.\n"
                "💡 Compara el CAT entre diferentes instituciones, no solo la tasa.\n"
                "________________________________________\n"
                "🔍 2. Comisiones escondidas\n"
                "Algunos créditos cobran por apertura, por manejo, por pagos tardíos o por pagos anticipados 😵\n"
                "📌 Lee siempre el contrato antes de firmar.\n"
                "________________________________________\n"
                "🔍 3. Tasa de interés variable\n"
                "📌 Algunos créditos no tienen tasa fija, sino que pueden subir.\n"
                "💡 Revisa si tu tasa es fija o variable. Las variables pueden volverse muy caras si sube la inflación.\n"
                "________________________________________\n"
                "🔍 4. Pago mensual bajo con plazo largo\n"
                "Parece atractivo, pero terminas pagando muchísimo más en intereses.\n"
                "________________________________________\n"
                "❗ Si el crédito parece demasiado fácil o rápido, pero no entiendes bien cuánto vas a pagar en total... ¡es una señal de alerta!\n\n"
                "¿Te gustaría que te ayude a comparar dos créditos que estés considerando?\n"
                "Solo dime los datos y lo vemos junt@s 😊"
            )
        if mensaje in ["7", "errores comunes al solicitar un crédito"]:
            return (
                "🟡 *Errores comunes al solicitar un crédito*\n"
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
                "✅ Haz un presupuesto antes de aceptar cualquier crédito.\n"
                "________________________________________\n\n"
                "¿Te gustaría que te ayude a planear cómo pagar tu crédito sin agobios?\n"
                "Solo dime y con gusto te oriento ✨"
            )
        if mensaje in ["8", "entender el buró de crédito"]:
            estado_usuario[numero] = {"esperando": "submenu_buro"}
            return (
                "🟡 *Entender el Buró de Crédito*\n"
                "El Buró de Crédito no es un enemigo, es solo un registro de cómo has manejado tus créditos. Y sí, puede ayudarte o perjudicarte según tu comportamiento.\n"
                "________________________________________\n"
                "📊 ¿Qué es el Buró de Crédito?\n"
                "Es una empresa que guarda tu historial de pagos.\n"
                "📌 Si pagas bien, tu historial será positivo.\n"
                "📌 Si te atrasas, se reflejará ahí.\n"
                "________________________________________\n"
                "💡 *Tener historial no es malo.*\n"
                "De hecho, si nunca has pedido un crédito, no aparecerás en Buró y eso puede dificultar que te aprueben uno.\n"
                "________________________________________\n"
                "📈 *Tu comportamiento crea un “score” o puntaje.*\n"
                "• Pagar a tiempo te ayuda\n"
                "• Deber mucho o atrasarte te baja el score\n"
                "• Tener muchas tarjetas al tope también afecta\n"
                "________________________________________\n"
                "❗ *Cuidado con estas ideas falsas:*\n"
                "• “Estoy en Buró” no siempre es malo\n"
                "• No es una lista negra\n"
                "• No te borran tan fácil (los registros duran años)\n"
                "________________________________________\n\n"
                "¿Te gustaría saber cómo mejorar tu historial crediticio o qué pasos tomar para subir tu puntaje?\n"
                "Solo dime *sí* y lo revisamos junt@s 😊"
            )

        if numero in estado_usuario and estado_usuario[numero].get("esperando") == "submenu_buro":
            if mensaje.strip().lower() == "sí":
                estado_usuario.pop(numero)
                return (
                    "📂 *Submenú: ¿Cómo mejorar mi historial crediticio?*\n"
                    "Aquí tienes algunos consejos prácticos para mejorar tu score en Buró de Crédito y tener un historial más saludable 📈\n"
                    "________________________________________\n"
                    "🔹 1. *Paga a tiempo, siempre*\n"
                    "📌 Aunque sea el pago mínimo, evita atrasarte.\n"
                    "✅ La puntualidad pesa mucho en tu historial.\n"
                    "________________________________________\n"
                    "🔹 2. *Usa tus tarjetas con moderación*\n"
                    "📌 Trata de no usar más del 30%-40% del límite de tu tarjeta.\n"
                    "✅ Usarlas hasta el tope te resta puntos, aunque pagues.\n"
                    "________________________________________\n"
                    "🔹 3. *No abras muchos créditos al mismo tiempo*\n"
                    "📌 Si pides varios préstamos en poco tiempo, parecerá que estás desesperado/a por dinero.\n"
                    "✅ Ve uno a la vez y maneja bien el que tienes.\n"
                    "________________________________________\n"
                    "🔹 4. *Usa algún crédito, aunque sea pequeño*\n"
                    "📌 Si no tienes historial, nunca tendrás score.\n"
                    "✅ Una tarjeta departamental o un plan telefónico pueden ser un buen inicio si los manejas bien.\n"
                    "________________________________________\n"
                    "🔹 5. *Revisa tu historial al menos una vez al año*\n"
                    "📌 Puedes pedir un reporte gratuito en www.burodecredito.com.mx\n"
                    "✅ Asegúrate de que no haya errores y de que tus datos estén correctos.\n"
                    "________________________________________\n"
                    "💡 ¿Quieres que te dé el link para pedir tu reporte de Buró gratis?\n"
                    "Solo dime *reporte* y te lo comparto."
                )

        if mensaje.strip().lower() == "reporte":
            return "Aquí tienes el enlace oficial para consultar tu reporte gratuito de Buró de Crédito: https://www.burodecredito.com.mx"
