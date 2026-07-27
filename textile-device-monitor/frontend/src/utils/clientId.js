const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/**
 * 生成 RFC 4122 v4 UUID。
 *
 * crypto.randomUUID() 只在安全上下文（HTTPS 或 localhost）中可用，因此
 * 局域网通过普通 HTTP 访问时优先退回 getRandomValues；极旧浏览器没有
 * Web Crypto 时仍生成格式合法的 UUID，确保幂等键不会因为页面环境失效。
 */
export const createClientUuid = (cryptoApi = globalThis.crypto) => {
  if (typeof cryptoApi?.randomUUID === 'function') {
    try {
      const uuid = cryptoApi.randomUUID();
      if (UUID_PATTERN.test(uuid)) {
        return uuid;
      }
    } catch {
      // 某些浏览器会在非安全上下文中暴露方法但调用时抛出异常。
    }
  }

  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === 'function') {
    cryptoApi.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }

  // RFC 4122 version 4 + variant 1 标志位。
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0'));
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10, 16).join(''),
  ].join('-');
};
