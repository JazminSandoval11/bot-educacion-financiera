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
getcontext().prec = 17  # Precisión igual a Excel

# Diccionario global para seguimiento de estados de usuarios
estado_usuario = {}

# =========================================
# Función: Cálculo de pago fijo (tipo Excel)
# =========================================
def calcular_pago_fijo_excel(monto, tasa, plazo):
    """
    Cálculo de pago fijo por periodo, equivalente a la función de Excel.
    """
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
    total_con_abonos = Decimal('0.00')  # ✅ Acumular aquí

    while saldo > 0:
        interes = saldo * r
        abono_a_capital = pago_fijo - interes

        # Si ya toca aplicar el abono extra
        if periodo >= desde:
            abono_a_capital += abono
            total_pago_periodo = pago_fijo + abono
        else:
            total_pago_periodo = pago_fijo

        # Si el abono cubre todo el saldo, calculamos el último pago
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
# Cálculo del costo real de compras a pagos fijos en tiendas departamentales
# =========================================
def calcular_costo_credito_tienda(precio_contado, pago_periodico, num_pagos):
    """
    Aproxima la tasa de interés si realizas pagos fijos por un producto
    y compara con el precio de contado.
    """
    precio = Decimal(str(precio_contado))
    cuota = Decimal(str(pago_periodico))
    n = int(num_pagos)

    saldo = precio
    r_estimada = Decimal('0.05')  # Valor inicial de iteración
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
    # Calcula tasa anual equivalente asumiendo 12 periodos
    tasa_anual = ((Decimal('1') + tasa_periodo) ** Decimal('12')) - Decimal('1')

    return (
        total_pagado.quantize(Decimal("0.01")),
        intereses.quantize(Decimal("0.01")),
        (tasa_periodo * 100).quantize(Decimal("0.01")),
        (tasa_anual * 100).quantize(Decimal("0.01"))
    )

# =========================================
# Saludo inicial con menú principal
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
    "8️⃣ Entender el Buró de Crédito"
)

# =========================================
# Función para enviar el mensaje
# (La real conectaría con la API de WhatsApp)
# =========================================
def enviar_mensaje(numero, texto):
    print(f"[Enviar a {numero}]: {texto}")

# =========================================
# Función principal de lógica de conversación
# =========================================
def procesar_mensaje(mensaje, numero):
    texto_limpio = mensaje.strip().lower()

    # Menú principal
    if texto_limpio in ["hola", "menu", "menú"]:
        estado_usuario[numero] = {}
        return saludo_inicial

    # Opción 1: Simular un crédito (flujo original)
    if texto_limpio in ["1", "simular un crédito"]:
        estado_usuario[numero] = {"esperando": "monto_credito"}
        return "Perfecto. Para comenzar, dime el monto del crédito que deseas simular."

    # Opción 2: Ver cuánto me ahorro si doy pagos extra (directo, sin pasar por 1)
    if texto_limpio in ["2", "ver cuánto me ahorro si doy pagos extra al crédito"]:
        # Creamos un estado separado para que pida
        # Monto, plazo, tasa y luego abono extra
        estado_usuario[numero] = {"esperando": "monto2"}
        return "Para estimar tu ahorro con pagos extra, primero dime el Monto del crédito."

    # Opción 3: Calcular costo real de compras a pagos fijos
    if texto_limpio in ["3", "calcular el costo real de compras a pagos fijos en tiendas departamentales"]:
        estado_usuario[numero] = {"esperando": "precio_contado"}
        return (
            "Vamos a calcular el costo real de una compra a pagos fijos.\n"
            "Por favor dime lo siguiente:\n\n"
            "1️⃣ ¿Cuál es el precio de contado del producto?"
        )

    # Opción 4: ¿Cuánto me pueden prestar?
    if texto_limpio in ["4", "¿cuánto me pueden prestar?"]:
        estado_usuario[numero] = {"esperando": "ingreso"}
        return (
            "Vamos a calcular cuánto podrías solicitar como crédito, según tu capacidad de pago.\n\n"
            "Primero necesito saber:\n"
            "1️⃣ ¿Cuál es tu ingreso neto mensual? (Después de impuestos y deducciones)"
        )

    # Opción 5: Consejos para pagar un crédito sin ahogarte
    if texto_limpio in ["5", "consejos para pagar un crédito sin ahogarte"]:
        return (
            "🟡 *Consejos para pagar un crédito sin ahogarte*\n"
            "1) Haz pagos anticipados\n"
            "2) Programa pagos en automático\n"
            "3) Revisa si puedes cambiar tu crédito\n"
            "4) Haz un presupuesto\n"
            "5) Prioriza deudas más caras\n"
            "Escribe *menú* para volver."
        )

    # Opción 6: Cómo identificar un crédito caro
    if texto_limpio in ["6", "cómo identificar un crédito caro"]:
        return (
            "🟡 *Cómo identificar un crédito caro*\n"
            "1) CAT elevado\n"
            "2) Comisiones escondidas\n"
            "3) Tasa variable\n"
            "4) Plazo largo con pagos bajos\n"
            "Si no entiendes el total a pagar, alerta.\n"
            "Escribe *menú* para volver."
        )

    # Opción 7: Errores comunes
    if texto_limpio in ["7", "errores comunes al solicitar un crédito"]:
        return (
            "🟡 *Errores comunes*\n"
            "1) No saber el total a pagar\n"
            "2) Pedir más de lo necesario\n"
            "3) Aceptar el primer crédito\n"
            "4) No leer contrato\n"
            "5) Usar crédito sin plan\n"
            "Escribe *menú* para volver."
        )

    # Opción 8: Buró de Crédito
    if texto_limpio in ["8", "entender el buró de crédito"]:
        estado_usuario[numero] = {"esperando": "submenu_buro"}
        return (
            "🟡 *Entender el Buró de Crédito*\n"
            "No es una lista negra, sino un registro.\n"
            "¿Te gustaría saber cómo mejorar tu historial?\n"
            "Responde *sí* o *no*."
        )

    # ============= LÓGICA DE ESTADOS =============
    if numero in estado_usuario and "esperando" in estado_usuario[numero]:
        contexto = estado_usuario[numero]

        # ==== FLUJO 2: Directo a abonos extra ====
        if contexto["esperando"] == "monto2":
            try:
                contexto["monto"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "plazo2"
                return "¿A cuántos pagos (periodos) lo piensas pagar?"
            except:
                return "Por favor, indica el monto del crédito como un número."

        if contexto["esperando"] == "plazo2":
            try:
                contexto["plazo"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa2"
                return "¿Cuál es la tasa de interés en el mismo periodo en que harás los pagos? (ej. 0.025 para 2.5%)"
            except:
                return "Por favor, indica el plazo como un número entero o decimal."

        if contexto["esperando"] == "tasa2":
            try:
                monto = contexto["monto"]
                plazo = contexto["plazo"]
                tasa = Decimal(mensaje.replace(",", ""))

                # Calculamos el pago fijo, guardamos en contexto
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
                    f"🧮 Y te ahorrarías ${ahorro} solo en intereses.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
            except:
                return "Ocurrió un error al calcular el ahorro. Revisa tus datos."

        # ==== FLUJO 1: Simulación original (monto_credito, plazo_credito, etc.) ====
        if contexto["esperando"] == "monto_credito":
            try:
                contexto["monto"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "plazo_credito"
                return "¿A cuántos pagos (periodos) lo piensas pagar?"
            except:
                return "Por favor, indica el monto como un número (ejemplo: 100000)"

        if contexto["esperando"] == "plazo_credito":
            try:
                contexto["plazo"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa_credito"
                return (
                    "¿Cuál es la tasa de interés en el mismo periodo en que harás los pagos?\n"
                    "Por ejemplo, si pagarás cada mes, la tasa debe ser mensual (0.025 para 2.5%)."
                )
            except:
                return "Por favor, indica el plazo como un número."

        if contexto["esperando"] == "tasa_credito":
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

        if contexto["esperando"] == "ver_si_abonos1":
            if texto_limpio == "sí":
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
                    f"🧮 Y te ahorrarías ${ahorro} solo en intereses.\n\n"
                    "Escribe *menú* para volver al inicio."
                )
            except:
                return "Ocurrió un error al calcular el ahorro. Revisa tus datos."

        # Opción 3: precio_contado, etc.
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
                return "3️⃣ ¿Cuántos pagos harás en total?"
            except:
                return "Cantidad inválida. Intenta con un número."

        if contexto["esperando"] == "numero_pagos_tienda":
            try:
                num_pagos = int(mensaje.strip())
                total, intereses, tasa_periodo, tasa_anual = calcular_costo_credito_tienda(
                    contexto["precio_contado"],
                    contexto["pago_fijo_tienda"],
                    num_pagos
                )
                estado_usuario.pop(numero)
                return (
                    f"📊 Aquí tienes los resultados:\n"
                    f"💰 Precio de contado: ${contexto['precio_contado']}\n"
                    f"📆 Pagos fijos de ${contexto['pago_fijo_tienda']} durante {num_pagos} periodos.\n\n"
                    f"💸 Total pagado: ${total}\n"
                    f"🧮 Intereses pagados: ${intereses}\n"
                    f"📈 Tasa por periodo: {tasa_periodo}%\n"
                    f"📅 Tasa anual equivalente: {tasa_anual}%\n\n"
                    "Escribe *menú* para volver al inicio."
                )
            except:
                return "Ocurrió un error al calcular el crédito. Revisa tus datos."

        # Opción 4: Ingreso, pagos_fijos, ...
        if contexto["esperando"] == "ingreso":
            try:
                contexto["ingreso"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "pagos_fijos"
                return (
                    "2️⃣ ¿Cuánto pagas mensualmente en créditos formales o instituciones financieras?\n"
                    "(No incluyas comida, renta, etc.)"
                )
            except:
                return "Por favor, escribe un número válido (ej: 12500)"

        if contexto["esperando"] == "pagos_fijos":
            try:
                contexto["pagos_fijos"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "deuda_revolvente"
                return "3️⃣ ¿Cuánto debes actualmente en tarjetas de crédito u otras deudas revolventes?"
            except:
                return "Por favor, indica la cantidad mensual que pagas en créditos (ej: 1800)"

        if contexto["esperando"] == "deuda_revolvente":
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

        if contexto["esperando"] == "riesgo":
            riesgo = texto_limpio
            if riesgo not in ["1", "2", "3"]:
                return "Elige 1, 2 o 3 según tu nivel de riesgo."

            contexto["riesgo"] = riesgo
            porcentaje_riesgo = {"1": Decimal("0.60"), "2": Decimal("0.45"), "3": Decimal("0.30")}[riesgo]
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

        if contexto["esperando"] == "subopcion_prestamo":
            opcion = texto_limpio
            if opcion == "1":
                contexto["esperando"] = "plazo_simular"
                return "📆 ¿A cuántos pagos (meses, quincenas, etc.) deseas simular el crédito?"
            elif opcion == "2":
                contexto["esperando"] = "monto_credito_deseado"
                return "💰 ¿De cuánto sería el crédito que te interesa solicitar?"
            else:
                return "Por favor, escribe 1 o 2."

        if contexto["esperando"] == "plazo_simular":
            try:
                contexto["plazo_simular"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa_simular"
                return "📈 ¿Cuál es la tasa de interés por periodo? (ej: 0.025 para 2.5%)"
            except:
                return "Número inválido."

        if contexto["esperando"] == "tasa_simular":
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
                    f"✅ Con base en tu capacidad de pago de ${capacidad}, podrías aspirar a un crédito de hasta ${monto_maximo}.\n\n"
                    "Escribe *menú* para volver."
                )
            except:
                return "Verifica tu tasa (ejemplo: 0.025)."

        if contexto["esperando"] == "monto_credito_deseado":
            try:
                contexto["monto_deseado"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "plazo_deseado"
                return "📆 ¿En cuántos pagos (meses, quincenas, etc.) planeas pagarlo?"
            except:
                return "Introduce un número válido."

        if contexto["esperando"] == "plazo_deseado":
            try:
                contexto["plazo_deseado"] = Decimal(mensaje.replace(",", ""))
                contexto["esperando"] = "tasa_deseada"
                return "📈 ¿Cuál es la tasa de interés por periodo? (ej: 0.025)"
            except:
                return "Número inválido."

        if contexto["esperando"] == "tasa_deseada":
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
                        f"✅ Puedes pagar este crédito sin problemas.\n"
                        f"Tu pago mensual estimado es ${pago_estimado}, dentro de tu capacidad (${capacidad}).\n"
                        "Escribe *menú* para volver."
                    )
                else:
                    diferencia = (pago_estimado - capacidad).quantize(Decimal("0.01"))
                    incremento_ingreso = (diferencia / porcentaje_riesgo).quantize(Decimal("0.01"))
                    reduccion_revolvente = (diferencia / Decimal("0.06")).quantize(Decimal("0.01"))
                    estado_usuario.pop(numero)
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

        # Submenú del Buró de Crédito
        if contexto["esperando"] == "submenu_buro":
            if texto_limpio == "sí":
                estado_usuario.pop(numero)
                return (
                    "📂 *Cómo mejorar tu historial crediticio*\n"
                    "1. Paga a tiempo (aunque sea mínimo)\n"
                    "2. Usa tarjetas con moderación (30-40% del límite)\n"
                    "3. No abras muchos créditos juntos\n"
                    "4. Usa algún crédito, aunque sea pequeño\n"
                    "5. Revisa tu historial al menos 1 vez al año (www.burodecredito.com.mx)\n\n"
                    "Escribe *menú*."
                )
            else:
                estado_usuario.pop(numero)
                return "Entiendo. Escribe *menú*."

    # Si nada aplica, menú por default:
    return "No entendí. Escribe *menú* para ver las opciones."

# =========================================
# Webhook de Flask para la API
# =========================================
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
