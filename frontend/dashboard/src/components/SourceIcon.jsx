import React from "react";
// Camera — ближайшая замена Instagram (lucide-react не содержит бренд-иконок)
// Share — ближайшая замена Facebook
// Video — ближайшая замена TikTok
import { Globe, Camera, Share, MapPin, Phone, MousePointer, Video, Link2 } from "lucide-react";

// Маппинг источников на иконки, цвета и метки
const SOURCE_CONFIG = {
  google_ads:      { icon: MousePointer, color: "#4285F4", label: "Google" },
  instagram:       { icon: Camera,       color: "#E4405F", label: "Instagram" },
  facebook:        { icon: Share,        color: "#1877F2", label: "Facebook" },
  tiktok:          { icon: Video,        color: "#010101", label: "TikTok" },
  "2gis_almaty":   { icon: MapPin,       color: "#0DAB76", label: "2GIS Алматы" },
  "2gis_astana":   { icon: MapPin,       color: "#0DAB76", label: "2GIS Астана" },
  "2gis_shymkent": { icon: MapPin,       color: "#0DAB76", label: "2GIS Шымкент" },
  "2gis_atyrau":   { icon: MapPin,       color: "#0DAB76", label: "2GIS Атырау" },
  "2gis_aktobe":   { icon: MapPin,       color: "#0DAB76", label: "2GIS Актобе" },
  // Taplink — кнопки "позвонить" на странице каждого города
  "taplink_almaty":   { icon: Link2,        color: "#FF5C00", label: "Taplink Алматы" },
  "taplink_astana":   { icon: Link2,        color: "#FF5C00", label: "Taplink Астана" },
  "taplink_shymkent": { icon: Link2,        color: "#FF5C00", label: "Taplink Шымкент" },
  "taplink_atyrau":   { icon: Link2,        color: "#FF5C00", label: "Taplink Атырау" },
  "taplink_aktobe":   { icon: Link2,        color: "#FF5C00", label: "Taplink Актобе" },
  // Instagram bio — номер прикреплён к ссылке в профиле aiplus.ent
  instagram_bio:   { icon: Camera,       color: "#E4405F", label: "Instagram bio" },
  site:            { icon: Globe,        color: "#3b82f6", label: "Сайт" },
  direct:          { icon: Phone,        color: "#94a3b8", label: "Прямой" },
};

export function SourceIcon({ source }) {
  const cfg = SOURCE_CONFIG[source] || SOURCE_CONFIG.direct;
  const Icon = cfg.icon;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
      <Icon size={14} color={cfg.color} />
      <span style={{ color: cfg.color }}>{cfg.label}</span>
    </span>
  );
}
