import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "kai trace",
  description: "kai のセッションログ（操作・実況）を Web で表示する",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ja">
      <body>{children}</body>
    </html>
  );
}
