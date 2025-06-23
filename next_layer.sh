#!/bin/bash
SD_PATH=~/printer_data/gcodes/
FILE_NAME=restore.gcode

cat ${2} > /tmp/tempF.$$
cat /tmp/tempF.$$ | sed -e '1,/;Z:'${1}'/ d' | > ${SD_PATH}/${FILE_NAME}
echo '_START_PRINT_RESTORE' >> ${SD_PATH}/${FILE_NAME}
tac /tmp/tempF.$$ | sed -e '/ Z'${1}'[^0-9]*$/q' | tac | tail -n+2 | sed -ne '/ Z/,$ p' >> ${SD_PATH}/${FILE_NAME}
rm /tmp/tempF.$$

#!/bin/bash
SD_PATH=~/printer_data/gcodes/
FILE_NAME=restore.gcode

cat ${2} > /tmp/tempF.$$
cat /tmp/tempF.$$ | sed -e '1,/;Z:'${1}'/ d' | > ${SD_PATH}/${FILE_NAME}
echo '_START_PRINT_RESTORE' >> ${SD_PATH}/${FILE_NAME}
tac /tmp/tempF.$$ | sed -e '/ Z'${1}'[^0-9]*$/q' | tac | tail -n+2 | sed -ne '/;Z:/,$ p' >> ${SD_PATH}/${FILE_NAME}   
rm /tmp/tempF.$$
