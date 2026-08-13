chmod +x /home/ssm-user/quant-run/wave2-validate-h2/basis_auto.sh
rm -f /home/ssm-user/quant-run/wave2-validate-h2/STOP
( crontab -l 2>/dev/null | grep -v basis_auto ; echo "*/5 * * * * /home/ssm-user/quant-run/wave2-validate-h2/basis_auto.sh" ) | crontab -
crontab -l | grep -q basis_auto && echo "ARMED — machine trigger installed, fires every 5 minutes"
/home/ssm-user/quant-run/wave2-validate-h2/basis_auto.sh
echo "first fire done; recent log:"
tail -15 /home/ssm-user/quant-run/wave2-validate-h2/auto.log
