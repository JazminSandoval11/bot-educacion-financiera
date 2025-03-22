from flask import Flask, request
import re

app = Flask(__name__)

VERIFY_TOKEN = "mi_token_secreto"

def simular_credito(monto, tasa_interes, plazo):
    if tasa_interes == 0:
        pago = monto / plazo
    else:
        pago = (monto * tasa_interes) / (1 - (1 + tasa_interes) ** -plazo)

    total_pagado = pago * plazo
    intereses_pagados = total_pagado - monto

    return {
        "pago": round(pago, 2),
        "total_pagado": round(total_pagado, 2),
        "intereses": round(intereses_pagados, 2)
    }

def responder_a_mensaje(mensaje):
    mensaje = mensaje.lower()

    if "simular crédito" in mensaje:
        try:
            patron = r"monto\s*=\s*(\d+\.?\d*),\s*plazo\s*=\s*(\d+),\s*tasa\s*=\s*(\d+\.?\d*)%"
            match = re.search(patron, mensaje)

            if match:
                monto = float(match.group(1))
                plazo = int(match.group(2))
                tasa = float(match.group(3)) / 100

                resultado = simular_credito(monto, tasa, plazo)

                return (
                    f"✅ Tus pagos por periodo serían de: ${resultado['pago']}\n"
                    f"💰 Total pagado: ${resultado['total_pagado']}\n"
                    f"🧮 Intereses pagados: ${resultado['intereses']}"
                )
            else:
                return (
                    "No pude entender los datos del crédito 😕\n"
                    "Por favor, escribe algo así:\n"
                    "simular crédito: monto=100000, plazo=12, tasa=2%"
                )
        except Exception as e:
            return f"Hubo un error en el cálculo: {e}"

    return "📚 Escribe 'simular crédito' seguido de los datos para ayudarte."

@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Token de verificación inválido", 403

@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    data = request.get_json()

    try:
        mensaje = data['entry'][0]['changes'][0]['value']['messages'][0]['text']['body']
        respuesta = responder_a_mensaje(mensaje)
        print("Mensaje recibido:", mensaje)
        print("Respuesta enviada:", respuesta)
    except Exception as e:
        print("Error al procesar mensaje:", e)

    return "OK", 200

app.run(host="0.0.0.0", port=3000)
