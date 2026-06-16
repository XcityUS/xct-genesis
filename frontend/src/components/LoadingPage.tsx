import { useTranslation } from 'react-i18next'

export default function LoadingPage() {
  const { t } = useTranslation()
  return (
    <div className="setup-page">
      <div className="setup-brand">{t('brand')}</div>
      <div className="setup-subtitle">{t('connecting')}</div>
    </div>
  )
}
