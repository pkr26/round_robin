import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Round Robin — A clearer next move",
  description:
    "An offline, fictional walkthrough of an evidence-led project comparison.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
