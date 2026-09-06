# view_db.py - Просмотр собранного датасета в реальном времени
import sqlite3
import time
from datetime import datetime

def watch_db():
    conn = sqlite3.connect("market_data.db")
    cursor = conn.cursor()
    
    print("👀 Наблюдение за датасетом (обновление каждые 1 сек). Нажми Ctrl+C для выхода.\n")
    print(f"{'ВРЕМЯ':<20} | {'ПАРА':<10} | {'ЦЕНА':<10} | {'ДИСБАЛАНС':<10} | {'СИГНАЛ'}")
    print("-" * 75)
    
    last_id = 0
    try:
        while True:
            cursor.execute("""
                SELECT id, timestamp, symbol, price, imbalance, signal 
                FROM market_snapshots 
                WHERE id > ? 
                ORDER BY id DESC LIMIT 10
            """, (last_id,))
            rows = cursor.fetchall()
            
            if rows:
                last_id = rows[0][0]
                
                for row in reversed(rows):
                    row_id, ts, symbol, price, imb, signal = row
                    time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                    
                    signal_color = "\033[92m" if signal == "BUY" else "\033[91m" if signal == "SELL" else "\033[0m"
                    reset = "\033[0m"
                    
                    print(f"{time_str:<20} | {symbol:<10} | ${price:<9.2f} | {imb:>+9.3f} | {signal_color}{signal:<6}{reset}")
            
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n🛑 Наблюдение остановлено.")
        conn.close()

if __name__ == "__main__":
    watch_db()
