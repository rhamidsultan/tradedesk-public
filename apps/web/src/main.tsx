import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import App from './App'

try { const t = localStorage.getItem('td-theme'); if (t) document.documentElement.dataset.theme = t } catch { /* storage unavailable */ }
createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
if ('serviceWorker' in navigator && location.protocol !== 'file:') navigator.serviceWorker.register('/sw.js').catch(() => {})
