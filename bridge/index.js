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
const WEB_PORT = process.env.PORT || 3000;

let currentQRDataUrl = null;
let currentStatus = "INICIALIZANDO";
let connectedNumber = null;

// Servidor Web local para mostrar el QR en alta definición en el navegador
const server = http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });

  let bodyContent = "";
  if (currentStatus === "CONECTADO") {
    bodyContent = `
      <div style="background: #d1e7dd; border: 2px solid #198754; color: #0f5132; padding: 30px; border-radius: 12px; text-align: center;">
        <h1 style="margin: 0 0 10px 0;">✔ ¡CONEXIÓN EXITOSA!</h1>
        <p style="font-size: 18px; margin: 0 0 15px 0;">El número dedicado <strong>${connectedNumber || ""}</strong> está vinculado a WhatsApp.</p>
        <p style="margin: 0; color: #0a3622;">Ya puedes agregar este número al grupo de WhatsApp de la guardia.<br>El asistente responderá automáticamente a los mensajes clínicos.</p>
      </div>
    `;
  } else if (currentQRDataUrl) {
    bodyContent = `
      <div style="text-align: center;">
        <h2 style="color: #1e3c72; margin-bottom: 5px;">📲 Vincular WhatsApp de Guardia</h2>
        <p style="color: #6c757d; font-size: 15px;">Escanea este código con el teléfono que tiene el número dedicado:</p>
        
        <div style="background: white; padding: 20px; display: inline-block; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); margin: 15px 0;">
          <img src="${currentQRDataUrl}" alt="Código QR de WhatsApp" style="width: 280px; height: 280px; display: block;" />
        </div>

        <div style="max-width: 450px; margin: 0 auto; text-align: left; background: #f8f9fa; padding: 15px 20px; border-radius: 8px; font-size: 14px;">
          <ol style="margin: 0; padding-left: 20px; line-height: 1.6;">
            <li>Abre <strong>WhatsApp</strong> en el teléfono dedicado.</li>
            <li>Toca <strong>Menú (⋮)</strong> o <strong>Ajustes (⚙)</strong>.</li>
            <li>Selecciona <strong>Dispositivos vinculados</strong>.</li>
            <li>Toca en <strong>Vincular un dispositivo</strong> y apunta la cámara a esta pantalla.</li>
          </ol>
        </div>
        <p style="margin-top: 15px; font-size: 12px; color: #adb5bd;">Esta página se actualiza automáticamente cada 3 segundos si el código se refresca.</p>
      </div>
    `;
  } else {
    bodyContent = `
      <div style="text-align: center; padding: 40px;">
        <h3>⏳ Generando nuevo código QR de WhatsApp...</h3>
        <p style="color: #6c757d;">Espere un momento mientras el servidor se conecta a WhatsApp.</p>
      </div>
    `;
  }

  const html = `<!DOCTYPE html>
  <html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Vincular WhatsApp de Guardia</title>
    <meta http-equiv="refresh" content="3">
    <style>
      body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #eef2f7; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }
      .card { background: white; border-radius: 16px; padding: 35px; box-shadow: 0 8px 24px rgba(0,0,0,0.06); max-width: 550px; width: 100%; }
    </style>
  </head>
  <body>
    <div class="card">${bodyContent}</div>
  </body>
  </html>`;

  res.end(html);
});

server.listen(WEB_PORT, () => {
  console.log(`\n======================================================`);
  console.log(`🌐 Interfaz Web de Vinculacion lista en: http://localhost:${WEB_PORT}`);
  console.log(`======================================================\n`);
  // Abrir navegador solo en entorno local Windows
  if (process.platform === "win32") {
    exec(`start http://localhost:${WEB_PORT}`, () => {});
  }
});

async function startWhatsAppBridge() {
  console.log("Iniciando WhatsApp Socket con Baileys...");
  const authDir = process.env.AUTH_DIR || "auth_session";
  const { state, saveCreds } = await useMultiFileAuthState(authDir);

  const sock = makeWASocket({
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: "silent" }),
    browser: ["Bitacora Medica", "Chrome", "1.0.0"],
  });

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
      console.log("\n[NUEVO QR GENERADO]: Se actualizo en http://localhost:3000");
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
      console.log("El bot ahora respondera a los mensajes del grupo de guardia.");
      console.log("========================================================\n");
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;

    for (const msg of messages) {
      if (msg.key.fromMe) continue;

      const chatId = msg.key.remoteJid;
      if (!chatId) continue;

      const text =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        "";

      if (!text.trim()) continue;

      try {
        const response = await fetch(`${BACKEND_URL}/whatsapp/webhook`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: text,
            sender: msg.key.participant || chatId,
            group_id: chatId,
          }),
        });

        if (response.ok) {
          const data = await response.json();
          if (data && data.reply) {
            await sock.sendMessage(chatId, { text: data.reply }, { quoted: msg });
            console.log(`[Respuesta enviada a ${chatId}]: ${data.reply}`);
          }
        }
      } catch (err) {
        console.error("Error contactando al backend de Bitacora:", err.message);
      }
    }
  });
}

startWhatsAppBridge().catch((err) => console.error("Error fatal:", err));
