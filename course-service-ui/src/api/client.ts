import { getCookie } from "./http";

export interface ApiError extends Error {
  response?: {
    status: number;
    type?: string;
    title?: string;
    detail?: string;
    extra?: Record<string, unknown>;
  };
}

const baseSettings: RequestInit = {
  credentials: "same-origin" as RequestCredentials,
  headers: {
    "X-XSRFToken": getCookie("_xsrf") || "",
  },
};

const handleResponse = async (response: Response): Promise<unknown> => {
  let data: Record<string, unknown>;
  try {
    data = await response.json();
  } catch {
    // Non-JSON response fallback
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`) as ApiError;
      error.response = { status: response.status };
      throw error;
    }
    return null;
  }

  if (!response.ok) {
    // Prefer RFC 9457 fields
    const errorMessage =
      (data.detail as string) ||
      (data.title as string) ||
      `HTTP ${response.status}`;
    const error = new Error(errorMessage) as ApiError;

    // Attach the whole problem details object for programmatic use
    error.response = {
      status: response.status,
      type: data.type as string,
      title: data.title as string,
      detail: data.detail as string,
      extra: { ...data }, // all other fields like course_id, term_id, etc.
    };

    throw error;
  }

  return data;
};

export const requests = {
  get: async (
    url: string,
    params: Record<string, string> | undefined = undefined,
  ): Promise<unknown> => {
    const settings: RequestInit = {
      ...baseSettings,
      method: "GET",
    };
    if (params !== undefined) {
      url += "?" + new URLSearchParams(params).toString();
    }
    const response = await fetch(url, settings);
    return handleResponse(response);
  },
  post: async (url: string, data: unknown): Promise<unknown> => {
    const settings: RequestInit = {
      ...baseSettings,
      method: "POST",
      body: JSON.stringify(data),
    };
    const response = await fetch(url, settings);
    return handleResponse(response);
  },
  put: async (url: string, data: unknown): Promise<unknown> => {
    const settings: RequestInit = {
      ...baseSettings,
      method: "PUT",
      body: JSON.stringify(data),
    };
    const response = await fetch(url, settings);
    return handleResponse(response);
  },
  del: async (url: string, data: unknown): Promise<unknown> => {
    const settings: RequestInit = {
      ...baseSettings,
      method: "DELETE",
      body: JSON.stringify(data),
    };
    const response = await fetch(url, settings);
    return handleResponse(response);
  },
};
