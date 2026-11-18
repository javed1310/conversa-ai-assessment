import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Conversa — Customer engagement workspace",
  description: "Configurable customer engagement application",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><head><link rel="preload" href="/fonts/display.woff2" as="font" type="font/woff2" crossOrigin="anonymous"/></head><body>{children}</body></html>;
}
