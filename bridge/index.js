import http from "node:http";
import { exec } from "node:child_process";
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import pino from "pino";
import QRCode from "qrcode";
import qrcodeTerminal from "qrcode-terminal";

const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8000";
const WEB_PORT = parseInt(process.env.PORT, 10) || 8080;

let currentQRDataUrl = null;
let currentStatus = "INICIALIZANDO";
let connectedNumber = null;
let currentPairingCode = null;
let globalSock = null;
let lastActiveChatId = null;

// Servidor Web local para mostrar el QR en alta definición en el navegador
function requestHandler(req, res) {
  const parsedUrl = new URL(req.url, `http://${req.headers.host || "localhost"}`);

  // Endpoint API: Estado en tiempo real para polling sin recargas de página
  if (parsedUrl.pathname === "/api/status") {
    res.writeHead(200, {
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
    });
    return res.end(
      JSON.stringify({
        status: currentStatus,
        qr: currentQRDataUrl,
        connectedNumber,
        pairingCode: currentPairingCode,
      })
    );
  }

  // Endpoint API: Solicitar código de vinculación por número de teléfono
  if (parsedUrl.pathname === "/api/pair" && req.method === "POST") {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", async () => {
      try {
        const payload = JSON.parse(body || "{}");
        const phone = (payload.phoneNumber || "").replace(/[^0-9]/g, "");
        if (!phone || phone.length < 8) {
          res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
          return res.end(
            JSON.stringify({ error: "Ingresa un número válido con código de país (ej. 58412... o 5939...)" })
          );
        }

        if (!globalSock || !globalSock.requestPairingCode) {
          res.writeHead(503, { "Content-Type": "application/json; charset=utf-8" });
          return res.end(
            JSON.stringify({ error: "El motor de WhatsApp aún se está conectando. Intenta de nuevo en 5 segundos." })
          );
        }

        const code = await globalSock.requestPairingCode(phone);
        currentPairingCode = code;
        console.log(`[PAIRING CODE]: Código generado para ${phone}: ${code}`);
        res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
        return res.end(JSON.stringify({ status: "ok", code }));
      } catch (err) {
        console.error("Error al generar pairing code:", err);
        res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
        return res.end(JSON.stringify({ error: err.message || "Error al solicitar código" }));
      }
    });
    return;
  }

  // Endpoint API: Envío proactivo de mensajes a WhatsApp (Recordatorios y Alertas de Robotina)
  if (parsedUrl.pathname === "/api/send-message" && req.method === "POST") {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", async () => {
      try {
        const payload = JSON.parse(body || "{}");
        const chatId = payload.chatId || lastActiveChatId;
        const text = payload.text;
        if (!chatId || !text) {
          res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
          return res.end(JSON.stringify({ error: "chatId y text son requeridos" }));
        }
        if (!globalSock) {
          res.writeHead(503, { "Content-Type": "application/json; charset=utf-8" });
          return res.end(JSON.stringify({ error: "Socket de WhatsApp no conectado aún" }));
        }
        await globalSock.sendMessage(chatId, { text });
        console.log(`[RECORDATORIO EMITIDO A ${chatId}]: "${text}"`);
        res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
        return res.end(JSON.stringify({ status: "ok" }));
      } catch (err) {
        console.error("Error enviando mensaje proactivo:", err.message);
        res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
        return res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  // Proxy transparente para descargas de PDF, HTML, debug y endpoints de backend
  if (
    parsedUrl.pathname.startsWith("/descargar/") ||
    parsedUrl.pathname.startsWith("/reports/") ||
    parsedUrl.pathname.startsWith("/debug/") ||
    parsedUrl.pathname === "/api/set-groq-key" ||
    parsedUrl.pathname === "/api/camas" ||
    parsedUrl.pathname === "/whatsapp/webhook"
  ) {
    const proxyReq = http.request(
      `${BACKEND_URL}${req.url}`,
      { method: req.method, headers: req.headers },
      (proxyRes) => {
        res.writeHead(proxyRes.statusCode, proxyRes.headers);
        proxyRes.pipe(res);
      }
    );
    proxyReq.on("error", (e) => {
      console.error("Error al redirigir peticion a FastAPI:", e.message);
      res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Error contactando al servidor backend.");
    });
    return req.pipe(proxyReq);
  }

  // Página Web Principal (HTML interactivo y reactivo sin recarga forzada)
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });

  const html = `<!DOCTYPE html>
  <html lang="es">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Robotina - Asistente Clínico de Guardia</title>
    <style>
      * { box-sizing: border-box; }
      body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
        background: #f0f4f8;
        display: flex;
        justify-content: center;
        align-items: center;
        min-height: 100vh;
        margin: 0;
        padding: 20px;
        color: #2c3e50;
      }
      .card {
        background: white;
        border-radius: 18px;
        padding: 35px;
        box-shadow: 0 10px 30px rgba(0,0,0,0.08);
        max-width: 580px;
        width: 100%;
        text-align: center;
      }
      .badge-bot {
        display: inline-block;
        background: #e0f2fe;
        color: #0369a1;
        padding: 6px 14px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 600;
        margin-bottom: 15px;
      }
      h1, h2 { margin: 0 0 10px 0; color: #1e293b; }
      p { margin: 0 0 15px 0; color: #64748b; font-size: 15px; line-height: 1.5; }
      
      .qr-container {
        background: white;
        padding: 20px;
        display: inline-block;
        border-radius: 14px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.08);
        margin: 15px 0;
        min-height: 290px;
        min-width: 290px;
      }
      .qr-container img {
        width: 280px;
        height: 280px;
        display: block;
        border-radius: 8px;
      }
      
      .steps {
        text-align: left;
        background: #f8fafc;
        padding: 16px 22px;
        border-radius: 10px;
        font-size: 14px;
        border-left: 4px solid #3b82f6;
        margin-top: 15px;
      }
      .steps ol { margin: 0; padding-left: 20px; line-height: 1.6; }
      
      .pulse-indicator {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        font-size: 13px;
        color: #10b981;
        margin-top: 12px;
        font-weight: 500;
      }
      .pulse-dot {
        width: 10px;
        height: 10px;
        background: #10b981;
        border-radius: 50%;
        box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
        animation: pulse 1.6s infinite;
      }
      @keyframes pulse {
        0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
        70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
        100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
      }

      .pairing-alt {
        margin-top: 25px;
        padding-top: 20px;
        border-top: 1px dashed #cbd5e1;
      }
      .pairing-input-row {
        display: flex;
        gap: 8px;
        margin-top: 10px;
      }
      .pairing-input-row input {
        flex: 1;
        padding: 10px 14px;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        font-size: 14px;
        outline: none;
      }
      .pairing-input-row input:focus {
        border-color: #3b82f6;
      }
      .pairing-input-row button {
        background: #0284c7;
        color: white;
        border: none;
        padding: 10px 16px;
        border-radius: 8px;
        cursor: pointer;
        font-weight: 600;
        font-size: 14px;
        white-space: nowrap;
      }
      .pairing-input-row button:hover {
        background: #0369a1;
      }
      .code-display {
        margin-top: 12px;
        background: #eff6ff;
        border: 2px dashed #3b82f6;
        padding: 12px;
        border-radius: 8px;
        font-family: monospace;
        font-size: 24px;
        letter-spacing: 4px;
        font-weight: bold;
        color: #1e40af;
      }

      .connected-box {
        background: #ecfdf5;
        border: 2px solid #10b981;
        color: #065f46;
        padding: 30px;
        border-radius: 14px;
        text-align: center;
      }
      .commands-box {
        margin-top: 20px;
        text-align: left;
        background: #ffffff;
        padding: 15px 20px;
        border-radius: 8px;
        border: 1px solid #a7f3d0;
        font-size: 13px;
      }
      .commands-box code {
        background: #d1fae5;
        color: #065f46;
        padding: 2px 6px;
        border-radius: 4px;
        font-weight: bold;
      }
    </style>
  </head>
  <body>
    <div class="card">
      <div class="badge-bot">🤖 Robotina • Bitácora Clínica 24/7</div>

      <!-- VISTA 1: CONECTADO -->
      <div id="view-connected" style="display: none;">
        <div class="connected-box">
          <div style="font-size: 48px; margin-bottom: 10px;">✔</div>
          <h2 style="color: #065f46;">¡ROBOTINA ESTÁ CONECTADA!</h2>
          <p style="font-size: 16px; color: #047857; margin-bottom: 15px;">
            WhatsApp activo con el número: <strong id="connected-phone"></strong>
          </p>
          <p style="color: #065f46; margin: 0;">
            El bot está escuchando y respondiendo en el grupo de guardia.
          </p>

          <div class="commands-box">
            <strong>💬 Mensajes que los residentes pueden enviar:</strong>
            <ul style="margin: 8px 0 0 0; padding-left: 20px; line-height: 1.7;">
              <li><code>Robotina la gasometria de 6a es pf 6.9</code> ➔ Registra el resultado.</li>
              <li><code>Robotina pendientes</code> ➔ Checklist de guardia nocturna.</li>
              <li><code>Robotina como esta 6a</code> ➔ Ficha y pendientes de la cama.</li>
              <li><code>Robotina camas</code> ➔ Lista de pacientes censados.</li>
              <li><code>Robotina ayuda</code> ➔ Guía de comandos.</li>
            </ul>
          </div>
        </div>
      </div>

      <!-- VISTA 2: ESPERANDO VINCULACIÓN (QR O CÓDIGO) -->
      <div id="view-linking">
        <h2>📲 Vincular WhatsApp</h2>
        <p>Apunta la cámara de WhatsApp a este código. La pantalla <strong>no parpadeará</strong> ni se recargará.</p>

        <div class="qr-container">
          <img id="qr-image" src="" alt="Generando Código QR..." style="display: none;" />
          <div id="qr-loading" style="padding-top: 110px; color: #94a3b8;">
            <div style="font-size: 32px; margin-bottom: 10px;">⏳</div>
            Conectando con WhatsApp...
          </div>
        </div>

        <div class="pulse-indicator">
          <div class="pulse-dot"></div>
          <span id="sync-text">Código QR estable y sincronizado en vivo</span>
        </div>

        <div class="steps">
          <ol>
            <li>Abre <strong>WhatsApp</strong> en el teléfono de la guardia.</li>
            <li>Toca <strong>Menú (⋮)</strong> o <strong>Ajustes (⚙)</strong>.</li>
            <li>Selecciona <strong>Dispositivos vinculados</strong>.</li>
            <li>Toca <strong>Vincular un dispositivo</strong> y enfoca la pantalla.</li>
          </ol>
        </div>

        <!-- OPCIÓN ALTERNATIVA: CÓDIGO DE 8 DÍGITOS SIN CÁMARA -->
        <div class="pairing-alt">
          <p style="margin-bottom: 6px; font-weight: 600; color: #334155; font-size: 14px;">
            ¿Prefieres vincular sin usar la cámara?
          </p>
          <p style="font-size: 13px; margin-bottom: 10px;">
            Ingresa tu número con código de país y te daremos un código de 8 dígitos para WhatsApp:
          </p>
          <div class="pairing-input-row">
            <input type="tel" id="input-phone" placeholder="Ej: 584121234567 o 593987654321" />
            <button id="btn-pair" onclick="solicitarCodigo()">Obtener Código</button>
          </div>
          <div id="pairing-code-box" style="display: none;">
            <p style="margin: 12px 0 4px 0; font-size: 13px; color: #1e40af;">
              En WhatsApp, toca <em>"Vincular con el número de teléfono"</em> e ingresa este código:
            </p>
            <div class="code-display" id="pairing-code-text"></div>
          </div>
          <div id="pairing-error" style="color: #ef4444; font-size: 13px; margin-top: 8px; display: none;"></div>
        </div>
      </div>
    </div>

    <script>
      let currentQR = "";

      async function checkStatus() {
        try {
          const res = await fetch('/api/status');
          if (!res.ok) return;
          const data = await res.json();

          if (data.status === 'CONECTADO') {
            document.getElementById('view-linking').style.display = 'none';
            document.getElementById('view-connected').style.display = 'block';
            document.getElementById('connected-phone').textContent = data.connectedNumber || 'Vinculado';
            return;
          }

          if (data.qr) {
            if (data.qr !== currentQR) {
              currentQR = data.qr;
              const img = document.getElementById('qr-image');
              img.src = data.qr;
              img.style.display = 'block';
              document.getElementById('qr-loading').style.display = 'none';
            }
          }

          if (data.pairingCode) {
            document.getElementById('pairing-code-box').style.display = 'block';
            document.getElementById('pairing-code-text').textContent = data.pairingCode;
          }
        } catch (e) {
          console.error("Error verificando estado:", e);
        }
      }

      async function solicitarCodigo() {
        const phoneInput = document.getElementById('input-phone');
        const phone = phoneInput.value.trim();
        const errDiv = document.getElementById('pairing-error');
        const btn = document.getElementById('btn-pair');
        errDiv.style.display = 'none';

        if (!phone) {
          errDiv.textContent = 'Por favor ingresa tu número con código de país.';
          errDiv.style.display = 'block';
          return;
        }

        btn.disabled = true;
        btn.textContent = 'Solicitando...';

        try {
          const res = await fetch('/api/pair', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phoneNumber: phone })
          });
          const data = await res.json();
          if (res.ok && data.code) {
            document.getElementById('pairing-code-box').style.display = 'block';
            document.getElementById('pairing-code-text').textContent = data.code;
          } else {
            errDiv.textContent = data.error || 'No se pudo generar el código. Revisa el número.';
            errDiv.style.display = 'block';
          }
        } catch (e) {
          errDiv.textContent = 'Error al contactar con el servidor.';
          errDiv.style.display = 'block';
        } finally {
          btn.disabled = false;
          btn.textContent = 'Obtener Código';
        }
      }

      // Consulta el estado cada 1.5 segundos sin recargar nunca la página
      setInterval(checkStatus, 1500);
      checkStatus();
    </script>
  </body>
  </html>`;

  res.end(html);
}

const server = http.createServer(requestHandler);

server.listen(WEB_PORT, "0.0.0.0", () => {
  console.log(`\n======================================================`);
  console.log(`🌐 Interfaz Web de Robotina lista en: http://0.0.0.0:${WEB_PORT}`);
  console.log(`======================================================\n`);
  if (process.platform === "win32") {
    exec(`start http://localhost:${WEB_PORT}`, () => {});
  }
});
server.on("error", (e) => console.error("Error en servidor principal:", e.message));

// Servidor de respaldo en 8080 si Railway configuró el proxy en 8080 y WEB_PORT fue otro
if (WEB_PORT !== 8080) {
  try {
    const backupServer = http.createServer(requestHandler);
    backupServer.listen(8080, "0.0.0.0", () => {
      console.log(`🌐 Servidor de respaldo activo en: http://0.0.0.0:8080`);
    });
    backupServer.on("error", (e) => console.log("Puerto 8080 no activo:", e.message));
  } catch (err) {
    console.log("No se pudo iniciar servidor 8080:", err.message);
  }
}

async function startWhatsAppBridge() {
  console.log("Iniciando WhatsApp Socket con Baileys...");
  const authDir = process.env.AUTH_DIR || "auth_session";
  const { state, saveCreds } = await useMultiFileAuthState(authDir);

  const sock = makeWASocket({
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: "silent" }),
    browser: ["Robotina Guardia", "Chrome", "1.0.0"],
  });

  globalSock = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      currentStatus = "ESPERANDO_QR";
      try {
        currentQRDataUrl = await QRCode.toDataURL(qr, { width: 300, margin: 2 });
      } catch (e) {
        console.error("Error generando imagen QR:", e);
      }
      console.log(`\n[NUEVO QR GENERADO]: Se actualizo en http://0.0.0.0:${WEB_PORT}`);
      qrcodeTerminal.generate(qr, { small: true });
    }

    if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log(`Conexion cerrada (codigo ${statusCode}). Reconectando: ${shouldReconnect}`);
      currentStatus = "DESCONECTADO";
      currentQRDataUrl = null;

      if (shouldReconnect) {
        setTimeout(startWhatsAppBridge, 3000);
      } else {
        console.log("Sesion cerrada por el usuario. Limpiando credenciales...");
      }
    } else if (connection === "open") {
      currentStatus = "CONECTADO";
      currentQRDataUrl = null;
      connectedNumber = sock.user?.id ? sock.user.id.split(":")[0] : "Vinculado";

      console.log("\n========================================================");
      console.log(`✔ CONEXION EXITOSA CON WHATSAPP! (${connectedNumber})`);
      console.log("Robotina ahora respondera a los mensajes del grupo de guardia.");
      console.log("========================================================\n");
    }
  });

  const mySentMessageIds = new Set();

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    // Procesar tanto mensajes de terceros ('notify') como del propio dispositivo vinculado ('append')
    if (type !== "notify" && type !== "append") return;

    for (const msg of messages) {
      if (mySentMessageIds.has(msg.key.id)) continue;

      const chatId = msg.key.remoteJid;
      if (!chatId || chatId === "status@broadcast") continue;
      lastActiveChatId = chatId;

      // Desempaquetar mensajes ordinarios, temporales (ephemeral) o multimedia
      const m =
        msg.message?.ephemeralMessage?.message ||
        msg.message?.viewOnceMessage?.message ||
        msg.message?.viewOnceMessageV2?.message ||
        msg.message?.documentWithCaptionMessage?.message ||
        msg.message;

      if (!m) continue;

      const text =
        m.conversation ||
        m.extendedTextMessage?.text ||
        m.imageMessage?.caption ||
        m.videoMessage?.caption ||
        "";

      if (!text.trim()) continue;

      // Si el mensaje viene del propio número vinculado (fromMe),
      // solo procesarlo si no es una respuesta del propio bot
      if (msg.key.fromMe) {
        if (/^[🚨✔📌👋🏥⚠🤖]/.test(text.trim())) continue;
      }

      console.log(`\n[MSG RECIBIDO] Chat: ${chatId} | De: ${msg.key.participant || chatId} | fromMe: ${msg.key.fromMe} | Type: ${type}`);
      console.log(`[TEXTO]: "${text}"`);

      try {
        const response = await fetch(`${BACKEND_URL}/whatsapp/webhook`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: text,
            sender: msg.key.participant || chatId,
            group_id: chatId,
            is_from_me: msg.key.fromMe,
          }),
        });

        if (response.ok) {
          const data = await response.json();
          if (data && data.reply) {
            // Evitar quoting a mensajes propios para prevenir rechazos del protocolo
            const sendOptions = msg.key.fromMe ? {} : { quoted: msg };
            const sent = await sock.sendMessage(chatId, { text: data.reply }, sendOptions);
            if (sent?.key?.id) {
              mySentMessageIds.add(sent.key.id);
            }
            console.log(`[RESPUESTA ENVIADA]: "${data.reply}"`);
          } else {
            console.log(`[IGNORADO POR BACKEND]: Motivo: ${data?.reason || "Sin coincidencia clínica"}`);
          }
        } else {
          console.error(`[ERROR HTTP BACKEND]: ${response.status} ${response.statusText}`);
        }
      } catch (err) {
        console.error("Error contactando al backend de Bitacora:", err.message);
      }
    }
  });
}

startWhatsAppBridge().catch((err) => console.error("Error fatal:", err));
