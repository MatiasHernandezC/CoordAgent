export async function sendAgentReply({ sock, jid, result, pendingItem, markProgress, logger }) {
  if (result.agent_reply_image && !pendingItem.primarySent) {
    const caption = result.agent_reply_caption || result.agent_reply || "";
    await sock.sendMessage(jid, {
      image: Buffer.from(result.agent_reply_image, "base64"),
      caption
    });
    markProgress({ primarySent: true });
    logger?.info?.({ jid, format: result.agent_reply_format }, "respuesta con imagen enviada al grupo");
  } else if (result.agent_reply && !pendingItem.primarySent) {
    await sock.sendMessage(jid, { text: result.agent_reply });
    markProgress({ primarySent: true });
    logger?.info?.({ jid, format: result.agent_reply_format }, "respuesta de texto enviada al grupo");
  }

  // Si falla el documento, el error se propaga. El caller conserva la cola y,
  // gracias a primarySent persistido, el reintento envia solo el adjunto.
  if (result.agent_reply_document && result.agent_reply_document_name && !pendingItem.documentSent) {
    await sock.sendMessage(jid, {
      document: Buffer.from(result.agent_reply_document, "base64"),
      fileName: result.agent_reply_document_name,
      mimetype: result.agent_reply_document_mimetype || "application/octet-stream"
    });
    markProgress({ documentSent: true });
    logger?.info?.({ jid, fileName: result.agent_reply_document_name }, "documento enviado al grupo");
  }
}
