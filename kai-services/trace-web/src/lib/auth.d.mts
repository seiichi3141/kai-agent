// auth.mjs の型宣言（middleware.ts が型付きで import するため）。
export function safeEqual(a: string, b: string): boolean;

export interface AuthInput {
  expected: string;
  bearer?: string | null;
  cookie?: string | null;
  query?: string | null;
}

export interface AuthResult {
  authorized: boolean;
  setCookie: string | null;
}

export function checkAuth(input: AuthInput): AuthResult;
