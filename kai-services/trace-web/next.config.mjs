/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // 読み取り専用ビューア。ビルド時の lint/型エラーで本番起動を止めない（CI 側で別途検査）。
  eslint: { ignoreDuringBuilds: true },
};
export default nextConfig;
