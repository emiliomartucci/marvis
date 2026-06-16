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

// v2 is default-on, so .theme-v2 is rendered SERVER-SIDE in <html className>
// below: a class React owns survives hydration-error recovery, while a class
// added only by a pre-hydration script gets dropped when React re-renders the
// root from scratch (that was the beta-0.3.9b1 "blue v1 skin" bug). The inline
// script only handles the explicit localStorage opt-out by REMOVING the class
// before first paint. Kept as an inline <script> (not <Script>) so Next's App
// Router runtime never defers it past hydration.
const designV2InitScript = `(function(){try{if(localStorage.getItem("marvisx:design-v2")==="false")document.documentElement.classList.remove("theme-v2");}catch(e){}})();`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${exo2.variable} ${jetbrainsMono.variable} theme-v2`}
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
