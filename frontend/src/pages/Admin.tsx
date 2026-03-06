import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Settings } from 'lucide-react';
import { SettingsPanel } from '../components/unified/SettingsPanel';

export default function Admin() {
  const navigate = useNavigate();

  return (
    <div className="h-screen flex flex-col bg-gray-900 text-white">
      {/* Header */}
      <header className="bg-gray-800 border-b border-gray-700 px-6 py-4 flex items-center gap-4">
        <button
          onClick={() => navigate('/')}
          className="p-2 hover:bg-gray-700 rounded-lg transition-colors"
        >
          <ArrowLeft className="w-5 h-5 text-gray-400" />
        </button>
        <Settings className="w-5 h-5 text-blue-400" />
        <h1 className="text-lg font-semibold">Admin Settings</h1>
      </header>

      <SettingsPanel />
    </div>
  );
}
