/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        edge: {
          green:  '#00e87a',
          'green-dim': '#00b85f',
          gold:   '#f59e0b',
          'gold-dim': '#b45309',
          red:    '#ff4d6d',
          blue:   '#3b82f6',
          purple: '#a855f7',
          cyan:   '#06b6d4',
        },
        ink: {
          950: '#03060c',
          900: '#070d19',
          850: '#0c1524',
          800: '#101d2e',
          750: '#152338',
          700: '#1c2d45',
          600: '#263a55',
          500: '#334e6e',
          400: '#4d6b8a',
          300: '#7090ad',
          200: '#a0b8cc',
          100: '#d0dde8',
        },
        surface: {
          900: '#0a0e14',
          800: '#111827',
          700: '#1f2937',
          600: '#374151',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
        display: ['Inter', 'system-ui', 'sans-serif'],
      },
      backgroundImage: {
        'card-gradient': 'linear-gradient(135deg, rgba(21,35,56,0.9) 0%, rgba(12,21,36,0.95) 100%)',
        'header-gradient': 'linear-gradient(90deg, #03060c 0%, #070d19 50%, #03060c 100%)',
        'green-glow': 'radial-gradient(ellipse at center, rgba(0,232,122,0.15) 0%, transparent 70%)',
      },
      boxShadow: {
        'card': '0 1px 0 0 rgba(255,255,255,0.04), 0 4px 16px rgba(0,0,0,0.4)',
        'card-hover': '0 1px 0 0 rgba(255,255,255,0.06), 0 8px 32px rgba(0,0,0,0.6)',
        'glow-green': '0 0 20px rgba(0,232,122,0.25)',
        'glow-gold': '0 0 20px rgba(245,158,11,0.25)',
        'inset-top': 'inset 0 1px 0 0 rgba(255,255,255,0.06)',
      },
      borderRadius: {
        'card': '12px',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fadeIn 0.4s ease forwards',
        'slide-up': 'slideUp 0.3s ease forwards',
      },
      keyframes: {
        fadeIn: { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
        slideUp: { '0%': { opacity: 0, transform: 'translateY(8px)' }, '100%': { opacity: 1, transform: 'translateY(0)' } },
      },
    },
  },
  plugins: [],
};
