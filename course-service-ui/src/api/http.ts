export const getCookie = (name: string): string | null => {
  let cookieValue: string | null = null;
  if (document.cookie && document.cookie !== "") {
    for (const cookie of document.cookie.split(";")) {
      if (cookie.trim().startsWith(`${name}=`)) {
        cookieValue = decodeURIComponent(cookie.trim().substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
};

export const urlJoin = (...parts: string[]): string => {
  parts = parts.map((part, index) => {
    if (index) {
      part = part.replace(/^\//, "");
    }
    if (index !== parts.length - 1) {
      part = part.replace(/\/$/, "");
    }
    return part;
  });
  return parts.join("/");
};
