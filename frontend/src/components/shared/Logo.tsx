import { useState } from 'react';
import { Phone } from 'lucide-react';

interface LogoProps {
  className?: string;
  size?: 'sm' | 'md' | 'lg';
}

const sizeMap = {
  sm: { img: 'h-8', icon: 'h-8 w-8' },
  md: { img: 'h-12', icon: 'h-12 w-12' },
  lg: { img: 'h-16', icon: 'h-16 w-16' },
};

export default function Logo({ className = '', size = 'md' }: LogoProps) {
  const [imgError, setImgError] = useState(false);
  const s = sizeMap[size];

  if (imgError) {
    return <Phone className={`${s.icon} text-blue-600 ${className}`} />;
  }

  return (
    <img
      src="/logo.png"
      alt="SignalWire Call Center"
      className={`${s.img} ${className}`}
      onError={() => setImgError(true)}
    />
  );
}
