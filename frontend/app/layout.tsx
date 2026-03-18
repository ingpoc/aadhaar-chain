import type { Metadata } from "next"
import { IBM_Plex_Mono, Inter, Instrument_Serif } from "next/font/google"
import type { ReactNode } from "react"

import "./globals.css"
import { ConditionalMainWrapper, ConditionalNavbar } from "@/components/layout/ConditionalNavbar"
import { Providers } from "@/components/providers/Providers"

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
})

const instrumentSerif = Instrument_Serif({
  subsets: ["latin"],
  variable: "--font-instrument-serif",
  weight: ["400"],
})

const ibmPlexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  variable: "--font-ibm-plex-mono",
  weight: ["400", "500"],
})

export const metadata: Metadata = {
  title: "Identity Agent Platform - Self-Sovereign Identity on Solana",
  description: "Decentralized identity platform powered by Solana and Claude Agent SDK",
}

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode
}>) {
  return (
    <html lang="en" data-scroll-behavior="smooth">
      <body
        className={`${inter.variable} ${instrumentSerif.variable} ${ibmPlexMono.variable}`}
      >
        <Providers>
          <div className="min-h-screen bg-background">
            <ConditionalNavbar />
            <ConditionalMainWrapper>{children}</ConditionalMainWrapper>
          </div>
        </Providers>
      </body>
    </html>
  )
}
