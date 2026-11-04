import Sidebar from '../components/Sidebar';
import Header from '../components/Header';

export default function Alerts() {
  const alerts = [
    {
      vehicle: 'Truck CA123456',
      severity: 'High',
      location: 'N1 Johannesburg'
    },
    {
      vehicle: 'Bus GP445789',
      severity: 'Medium',
      location: 'Pretoria Toll Gate'
    }
  ];

  return (
    <div className='layout'>
      <Sidebar />

      <main className='content'>
        <Header />

        <section className='panel'>
          <h2>Vehicle Overload Alerts</h2>

          {alerts.map((alert, index) => (
            <div className='alert-card' key={index}>
              <h3>{alert.vehicle}</h3>
              <p>Severity: {alert.severity}</p>
              <p>Location: {alert.location}</p>
            </div>
          ))}
        </section>
      </main>
    </div>
  );
}