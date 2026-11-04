export default function Header() {
  return (
    <header className='header'>
      <div>
        <h1>Vehicle Overload Detection System</h1>
        <p>YOLOv11 + Raspberry Pi 5 + FastAPI</p>
      </div>

      <div className='status'>🟢 Camera Connected</div>
    </header>
  );
}