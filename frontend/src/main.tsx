import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import OfflineCleanupNotice from './features/offline/OfflineCleanupNotice'
import './features/offline/vault.ts'
import './styles.css'
import './workspace.css'
import {registerPwa} from './features/pwa/register'
registerPwa()
const client = new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:true}}})
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><QueryClientProvider client={client}><OfflineCleanupNotice/><App/></QueryClientProvider></React.StrictMode>)
