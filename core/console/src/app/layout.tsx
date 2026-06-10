import type { Metadata, Viewport } from "next";
import { Exo_2, JetBrains_Mono } from "next/font/google";
import "@/styles/globals.css";
import { ThemeProvider } from "next-themes";
import { APP_BASE_PATH, MANIFEST_PATH } from "@/lib/config";

// Exo 2 e' il sans canonico per UI + display dal 2026-05-25 (design system
// colors_and_type.css). Tutti i pesi 100-900 + italic via next/font/google
// (self-hosted automatic da Next, no runtime request a Google al render).
// Il @font-face locale in globals.css copre il caso build offline.
const exo2 = Exo_2({
  weight: ["100", "200", "300", "400", "500", "600", "700", "800", "900"],
  style: ["normal", "italic"],
  subsets: ["latin"],
  variable: "--font-exo-2",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  weight: ["400", "500", "600"],
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
  display: "fallback",
});

export const metadata: Metadata = {
  title: "Marvis Console",
  description: "Agent-native project management console",
  manifest: MANIFEST_PATH,
  appleWebApp: {
    capable: true,
    title: "Marvis",
    statusBarStyle: "default",
  },
  icons: {
    icon: [
      { url: `${APP_BASE_PATH}/favicon-16.svg`, sizes: "16x16", type: "image/svg+xml" },
      { url: `${APP_BASE_PATH}/favicon-32.svg`, sizes: "32x32", type: "image/svg+xml" },
      { url: `${APP_BASE_PATH}/favicon.svg`, type: "image/svg+xml" },
    ],
    apple: [{ url: `${APP_BASE_PATH}/icons/icon-192.png`, sizes: "192x192" }],
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  themeColor: "#0f172a",
};

// Pre-hydration script: applies .theme-v2 class before first paint to avoid FOUC.
// v2 is default-on; only an explicit localStorage "false" opts back to v1.
// Kept as an inline <script> (not <Script>) so Next's App Router runtime never
// defers it past hydration.
const designV2InitScript = `(function(){try{if(localStorage.getItem("marvisx:design-v2")!=="false")document.documentElement.classList.add("theme-v2");}catch(e){document.documentElement.classList.add("theme-v2");}})();`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${exo2.variable} ${jetbrainsMono.variable}`}
    >
      <head>
        <script
          id="pir-design-v2-init"
          dangerouslySetInnerHTML={{ __html: designV2InitScript }}
        />
      </head>
      <body className="bg-pir-base text-pir-text-primary font-sans antialiased h-screen overflow-hidden">
        <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
