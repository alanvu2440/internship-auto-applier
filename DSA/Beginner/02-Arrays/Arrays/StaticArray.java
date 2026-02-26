package DSA.Beginner.Arrays;

public class StaticArray {
    //Insert n into arr at next open position 
    public void insertEnd(int[] arr, int n, int length, int capacity) { 
        // length not length + 1 bc length is always one ahead of the index
        if (length < capacity) {
            arr[length] = n;
        }
    }

    //Remove from the last position in the array
    public void removeEnd(int[] arr, int length) {
        if (length > 0) {
            arr[length - 1] = 0;
            //length--
        }
    }

    // Insert n into index i after shifting elements to the right.
    public void insertMiddle(int[] arr, int i, int n,  int length) {
        //shift starting from the end to i, bc if we start with i, i will overwrite i+1
        // [0,1,2,3,4,5]  length = 6, i = 2

        //this is only assuming that we have no more room
        // for (int index = length - 1; index >= i ; index--) {
        //     arr[index] = arr[index - 1];
        // } 
        // arr[i] = n; 

        // [0,1,2,3,4,5,0]  length = 7, i = 2
        for (int index = length - 1; index >= i; index--) {
            //first entry gets skipped
            arr[index + 1] = arr[index];
        }
        arr[i] = n; 

    }

    // Remove value at index i before shifting elements to the left.
    public void removeMiddle(int[] arr, int i, int length) {
        /* [0,1,2,3,4,5]  length = 6, i = 2, index = 3
        [0,1,3,4,5,0]
        */ 
        // start from i then go to end
        for (int index = i + 1; index < length; index++) {
            arr[i] = arr[index];
            i++;
            //arr [index -1] = arr[index] works too
        }
        
    }

    public void printArr(int[] arr, int length) {
        for (int i = 0; i < length; i++) {
            System.out.print(arr[i] + " ");
        }
        System.out.println();
    }
}

